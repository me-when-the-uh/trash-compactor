use flate2::write::ZlibEncoder;
use flate2::Compression;
use memmap2::Mmap;
use pyo3::prelude::*;
use rayon::prelude::*;
use rayon::ThreadPoolBuilder;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

const LZ4_INCOMPRESSIBLE_THRESHOLD: f64 = 0.95;

const ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE: u64 = 2 * 1024 * 1024;
const ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE: u64 = 100 * 1024 * 1024;
const ENTROPY_BASE_SAMPLE_WINDOWS: u32 = 3;
const ENTROPY_DYNAMIC_WINDOWS_MIN: u32 = 4;
const ENTROPY_DYNAMIC_WINDOWS_MAX: u32 = 20;
const ENTROPY_TARGET_WINDOW_SIZE: u64 = 16 * 1024;

#[pyclass]
#[derive(Clone)]
pub struct DirEntropyResult {
    #[pyo3(get)]
    pub dir: String,
    #[pyo3(get)]
    pub average_entropy: f64,
    #[pyo3(get)]
    pub sampled_files: u32,
    #[pyo3(get)]
    pub sampled_bytes: u64,
    #[pyo3(get)]
    pub lz4_certain: u32,
}

struct FileProbeJob {
    path: String,
    size: u64,
    budget: u64,
    order: u32,
}

struct DirPlan {
    dir: String,
    jobs: Vec<FileProbeJob>,
}

struct FileProbeOutcome {
    order: u32,
    weighted_entropy: f64,
    sampled_bytes: u64,
    lz4_certain: bool,
}

fn get_sample_window_count(file_size: u64) -> u32 {
    if file_size <= ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE {
        return ENTROPY_BASE_SAMPLE_WINDOWS;
    }
    if file_size >= ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE {
        return ENTROPY_DYNAMIC_WINDOWS_MAX;
    }
    let ratio = (file_size - ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE) as f64
        / (ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE - ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE) as f64;
    ENTROPY_DYNAMIC_WINDOWS_MIN
        + (ratio * (ENTROPY_DYNAMIC_WINDOWS_MAX - ENTROPY_DYNAMIC_WINDOWS_MIN) as f64) as u32
}

fn derive_window_size(byte_budget: u64, num_windows: u32) -> u64 {
    if byte_budget == 0 {
        return 0;
    }
    let mut window = ENTROPY_TARGET_WINDOW_SIZE.min(byte_budget);
    if window * num_windows as u64 > byte_budget && byte_budget >= num_windows as u64 {
        window = byte_budget / num_windows as u64;
    }
    window.max(1)
}

fn plan_sample_windows(file_size: u64, window_size: u64, num_windows: u32) -> Vec<(u64, u64)> {
    if file_size == 0 || window_size == 0 || num_windows == 0 {
        return Vec::new();
    }

    if file_size <= window_size {
        return vec![(0, file_size)];
    }

    let mut raw_windows: Vec<(u64, u64)> = Vec::new();

    if num_windows == 3 && ENTROPY_BASE_SAMPLE_WINDOWS == 3 {
        let p10 = (file_size as f64 * 0.10) as u64;
        let p45 = (file_size as f64 * 0.45) as u64;
        let p80 = (file_size as f64 * 0.80) as u64;

        let w1_start = p10;
        let w2_start = p45.saturating_sub(window_size / 2);
        let w3_start = p80.saturating_sub(window_size);

        for start in [w1_start, w2_start, w3_start] {
            let start = start.min(file_size);
            let end = (start + window_size).min(file_size);
            if end > start {
                raw_windows.push((start, end));
            }
        }
    } else {
        let max_start = file_size.saturating_sub(window_size);
        let step = if num_windows > 1 {
            max_start as f64 / (num_windows - 1) as f64
        } else {
            0.0
        };
        for i in 0..num_windows {
            let start = (i as f64 * step) as u64;
            let end = (start + window_size).min(file_size);
            if end > start {
                raw_windows.push((start, end));
            }
        }
    }

    if raw_windows.is_empty() {
        return Vec::new();
    }

    raw_windows.sort_unstable_by_key(|(s, _)| *s);
    let mut merged: Vec<(u64, u64)> = Vec::new();
    let (mut current_start, mut current_end) = raw_windows[0];

    for (next_start, next_end) in raw_windows.into_iter().skip(1) {
        if next_start <= current_end {
            current_end = current_end.max(next_end);
        } else {
            merged.push((current_start, current_end - current_start));
            current_start = next_start;
            current_end = next_end;
        }
    }
    merged.push((current_start, current_end - current_start));
    merged
}

fn compression_probe_entropy(sample: &[u8]) -> (f64, bool) {
    if sample.is_empty() {
        return (0.0, false);
    }

    if let Ok(compressed) = lz4::block::compress(sample, None, false) {
        if compressed.len() as f64 / sample.len() as f64 >= LZ4_INCOMPRESSIBLE_THRESHOLD {
            return (8.0, true);
        }
    }

    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(2));
    if encoder.write_all(sample).is_err() {
        return (8.0, false);
    }
    let Ok(compressed) = encoder.finish() else {
        return (8.0, false);
    };

    let compressed_length = compressed.len().max(1);
    let ratio = compressed_length as f64 / sample.len() as f64;
    let entropy = (ratio * 8.0).clamp(0.0, 8.0);
    (entropy, false)
}

fn probe_sample(data: &[u8]) -> (f64, bool) {
    compression_probe_entropy(data)
}

fn read_window_bytes(file: &mut File, mmap: Option<&Mmap>, offset: u64, length: u64) -> Vec<u8> {
    if let Some(map) = mmap {
        let start = offset as usize;
        let end = (offset + length).min(map.len() as u64) as usize;
        if start >= end {
            return Vec::new();
        }
        return map[start..end].to_vec();
    }

    use std::io::{Read, Seek, SeekFrom};
    let mut buf = vec![0u8; length as usize];
    if file.seek(SeekFrom::Start(offset)).is_err() {
        return Vec::new();
    }
    match file.read(&mut buf) {
        Ok(n) => {
            buf.truncate(n);
            buf
        }
        Err(_) => Vec::new(),
    }
}

fn probe_file(path: &str, file_size: u64, byte_budget: u64) -> (f64, u64, bool) {
    if byte_budget == 0 || file_size == 0 {
        return (0.0, 0, false);
    }

    let num_windows = get_sample_window_count(file_size);
    let window_size = derive_window_size(byte_budget, num_windows);
    let windows = plan_sample_windows(file_size, window_size, num_windows);
    if windows.is_empty() {
        return (0.0, 0, false);
    }

    let mut file = File::open(path).ok();
    let mmap = if file_size >= ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE {
        file.as_ref()
            .and_then(|f| unsafe { Mmap::map(f).ok() })
    } else {
        None
    };

    let mut weighted_entropy = 0.0f64;
    let mut sampled_bytes = 0u64;
    let mut sampled_chunks = 0u32;
    let mut lz4_shortcircuit_chunks = 0u32;
    for (offset, length) in windows {
        let remaining = byte_budget.saturating_sub(sampled_bytes);
        if remaining == 0 {
            break;
        }
        let read_len = length.min(remaining);
        if read_len == 0 {
            break;
        }

        let data = match &mut file {
            Some(f) => read_window_bytes(f, mmap.as_ref(), offset, read_len),
            None => Vec::new(),
        };
        if data.is_empty() {
            continue;
        }

        let (entropy, lz4_shortcircuit) = probe_sample(&data);
        let chunk_len = data.len() as u64;
        weighted_entropy += entropy * chunk_len as f64;
        sampled_bytes += chunk_len;
        sampled_chunks += 1;
        if lz4_shortcircuit {
            lz4_shortcircuit_chunks += 1;
        }

        if sampled_bytes >= byte_budget {
            break;
        }
    }

    let lz4_certain =
        sampled_chunks > 0 && lz4_shortcircuit_chunks == sampled_chunks;
    (weighted_entropy, sampled_bytes, lz4_certain)
}

fn collect_subtree_files(root: &Path, include_subdirectories: bool, breadth_first: bool) -> Vec<(String, u64)> {
    let mut files: Vec<(String, u64)> = Vec::new();
    let mut pending: Vec<PathBuf> = vec![root.to_path_buf()];
    // FIFO walk for sequential (HDD) probing keeps the disk head moving in
    // discovery order; LIFO is fine when probes run in parallel anyway.
    let mut head = 0usize;

    while head < pending.len() {
        let dir = if breadth_first {
            let d = pending[head].clone();
            head += 1;
            d
        } else {
            pending.pop().unwrap()
        };
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };

        for entry in entries.flatten() {
            let Ok(metadata) = entry.metadata() else {
                continue;
            };

            if metadata.is_dir() {
                if include_subdirectories {
                    pending.push(entry.path());
                }
                continue;
            }

            if !metadata.is_file() {
                continue;
            }

            let size = metadata.len();
            if size == 0 {
                continue;
            }

            files.push((entry.path().to_string_lossy().into_owned(), size));
        }
    }

    files
}

fn reservoir_sample(files: &[(String, u64)], max_files: u32) -> Vec<(String, u64)> {
    if max_files == 0 || files.is_empty() {
        return Vec::new();
    }

    // Deterministic selection: sort by size descending, take top N.
    let mut sorted: Vec<(String, u64)> = files.to_vec();
    sorted.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    sorted.truncate(max_files as usize);
    sorted
}

fn build_probe_jobs(
    sampled_list: &[(String, u64)],
    chunk_size: u64,
    max_bytes: u64,
) -> Vec<FileProbeJob> {
    let mut jobs = Vec::new();
    let mut remaining = max_bytes;

    for (order, (path, size)) in sampled_list.iter().enumerate() {
        if remaining == 0 {
            break;
        }
        let budget = chunk_size.min(remaining);
        if budget == 0 {
            break;
        }
        jobs.push(FileProbeJob {
            path: path.clone(),
            size: *size,
            budget,
            order: order as u32,
        });
        remaining = remaining.saturating_sub(budget);
    }

    jobs
}

fn prepare_directory_plan(
    dir: &str,
    max_files: u32,
    chunk_size: u64,
    max_bytes: u64,
    include_subdirectories: bool,
    breadth_first: bool,
) -> DirPlan {
    let root = Path::new(dir);
    let files = collect_subtree_files(root, include_subdirectories, breadth_first);
    let sampled_list = reservoir_sample(&files, max_files);
    let jobs = build_probe_jobs(&sampled_list, chunk_size, max_bytes);
    DirPlan {
        dir: dir.to_string(),
        jobs,
    }
}

fn aggregate_directory(
    outcomes: &mut [FileProbeOutcome],
    max_bytes: u64,
) -> (f64, u32, u64, u32) {
    outcomes.sort_unstable_by_key(|o| o.order);

    let mut sampled_files = 0u32;
    let mut sampled_bytes = 0u64;
    let mut weighted_entropy = 0.0f64;
    let mut lz4_certain_files = 0u32;

    for outcome in outcomes.iter() {
        if sampled_bytes >= max_bytes {
            break;
        }
        if outcome.sampled_bytes == 0 {
            continue;
        }

        sampled_files += 1;
        sampled_bytes += outcome.sampled_bytes;
        weighted_entropy += outcome.weighted_entropy;
        if outcome.lz4_certain {
            lz4_certain_files += 1;
        }

        if sampled_bytes >= max_bytes {
            break;
        }
    }

    if sampled_bytes == 0 {
        return (-1.0, sampled_files, sampled_bytes, lz4_certain_files);
    }

    let average_entropy = weighted_entropy / sampled_bytes as f64;
    (
        average_entropy,
        sampled_files,
        sampled_bytes,
        lz4_certain_files,
    )
}

fn fire_progress(
    callback: &Option<Py<PyAny>>,
    dir: &str,
    completed: usize,
    total: usize,
    sampled_files: u32,
) {
    let Some(cb) = callback else {
        return;
    };
    Python::with_gil(|py| {
        let _ = cb.call1(py, (dir, completed, total, sampled_files));
    });
}

fn maybe_fire_progress(
    callback: &Option<Py<PyAny>>,
    dir: &str,
    completed: usize,
    total: usize,
    sampled_files: u32,
    progress_lock: &Arc<Mutex<()>>,
    interval: usize,
) {
    if completed % interval != 0 && completed != total {
        return;
    }
    let _guard = progress_lock.lock().unwrap_or_else(|e| e.into_inner());
    fire_progress(callback, dir, completed, total, sampled_files);
}

#[pyfunction]
#[pyo3(signature = (dirs, max_files, max_bytes, chunk_size, include_subdirectories, workers, progress_callback=None))]
pub fn probe_directories_parallel(
    py: Python<'_>,
    dirs: Vec<String>,
    max_files: u32,
    max_bytes: u64,
    chunk_size: u64,
    include_subdirectories: bool,
    workers: usize,
    progress_callback: Option<Py<PyAny>>,
) -> PyResult<Vec<DirEntropyResult>> {
    if dirs.is_empty() {
        return Ok(Vec::new());
    }

    let total = dirs.len();
    let callback = progress_callback;
    let progress_lock = Arc::new(Mutex::new(()));
    let progress_interval = (total / 32).max(1);

    // One worker = sequential probing in discovery order (HDD mode). Parallel
    // probes would scatter the disk head across directories and files, which
    // on a spinning drive costs far more IOPS than it gains.
    if workers <= 1 {
        let results: Vec<DirEntropyResult> = py.allow_threads(|| {
            let mut results = Vec::with_capacity(total);
            for (done, dir) in dirs.iter().enumerate() {
                let plan = prepare_directory_plan(
                    dir,
                    max_files,
                    chunk_size,
                    max_bytes,
                    include_subdirectories,
                    true,
                );
                let mut outcomes: Vec<FileProbeOutcome> = Vec::new();
                for job in &plan.jobs {
                    let (weighted_entropy, sampled_bytes, lz4_certain) =
                        probe_file(&job.path, job.size, job.budget);
                    outcomes.push(FileProbeOutcome {
                        order: job.order,
                        weighted_entropy,
                        sampled_bytes,
                        lz4_certain,
                    });
                }
                let (average_entropy, sampled_files, sampled_bytes, lz4_certain) =
                    aggregate_directory(&mut outcomes, max_bytes);
                results.push(DirEntropyResult {
                    dir: plan.dir.clone(),
                    average_entropy,
                    sampled_files,
                    sampled_bytes,
                    lz4_certain,
                });
                maybe_fire_progress(
                    &callback,
                    &plan.dir,
                    done + 1,
                    total,
                    sampled_files,
                    &progress_lock,
                    progress_interval,
                );
            }
            results
        });
        return Ok(results);
    }

    let pool = ThreadPoolBuilder::new()
        .num_threads(workers.max(1))
        .build()
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;

    let results: Vec<DirEntropyResult> = py.allow_threads(|| {
        pool.install(|| {
            let plans: Vec<DirPlan> = dirs
                .par_iter()
                .map(|dir| {
                    prepare_directory_plan(
                        dir,
                        max_files,
                        chunk_size,
                        max_bytes,
                        include_subdirectories,
                        false,
                    )
                })
                .collect();

            let mut flat_jobs: Vec<(usize, FileProbeJob)> = Vec::new();
            for (dir_index, plan) in plans.iter().enumerate() {
                for job in &plan.jobs {
                    flat_jobs.push((
                        dir_index,
                        FileProbeJob {
                            path: job.path.clone(),
                            size: job.size,
                            budget: job.budget,
                            order: job.order,
                        },
                    ));
                }
            }

            let jobs_per_dir: Vec<usize> = plans.iter().map(|plan| plan.jobs.len()).collect();
            let grouped: Vec<Arc<Mutex<Vec<FileProbeOutcome>>>> = (0..plans.len())
                .map(|_| Arc::new(Mutex::new(Vec::new())))
                .collect();
            let result_slots: Vec<Arc<Mutex<Option<DirEntropyResult>>>> = (0..plans.len())
                .map(|_| Arc::new(Mutex::new(None)))
                .collect();
            let dirs_completed = Arc::new(AtomicUsize::new(0));

            for (dir_index, plan) in plans.iter().enumerate() {
                if plan.jobs.is_empty() {
                    let (average_entropy, sampled_files, sampled_bytes, lz4_certain) =
                        aggregate_directory(&mut Vec::new(), max_bytes);
                    *result_slots[dir_index].lock().unwrap_or_else(|e| e.into_inner()) =
                        Some(DirEntropyResult {
                            dir: plan.dir.clone(),
                            average_entropy,
                            sampled_files,
                            sampled_bytes,
                            lz4_certain,
                        });
                    let done = dirs_completed.fetch_add(1, Ordering::Relaxed) + 1;
                    maybe_fire_progress(
                        &callback,
                        &plan.dir,
                        done,
                        total,
                        sampled_files,
                        &progress_lock,
                        progress_interval,
                    );
                }
            }

            flat_jobs.par_iter().for_each(|(dir_index, job)| {
                let (weighted_entropy, sampled_bytes, lz4_certain) =
                    probe_file(&job.path, job.size, job.budget);
                let outcome = FileProbeOutcome {
                    order: job.order,
                    weighted_entropy,
                    sampled_bytes,
                    lz4_certain,
                };

                let mut outcomes = grouped[*dir_index]
                    .lock()
                    .unwrap_or_else(|e| e.into_inner());
                outcomes.push(outcome);
                if outcomes.len() != jobs_per_dir[*dir_index] {
                    return;
                }

                let (average_entropy, sampled_files, sampled_bytes, lz4_certain) =
                    aggregate_directory(&mut outcomes, max_bytes);
                let plan_dir = plans[*dir_index].dir.clone();
                *result_slots[*dir_index].lock().unwrap_or_else(|e| e.into_inner()) =
                    Some(DirEntropyResult {
                        dir: plan_dir.clone(),
                        average_entropy,
                        sampled_files,
                        sampled_bytes,
                        lz4_certain,
                    });
                drop(outcomes);

                let done = dirs_completed.fetch_add(1, Ordering::Relaxed) + 1;
                maybe_fire_progress(
                    &callback,
                    &plan_dir,
                    done,
                    total,
                    sampled_files,
                    &progress_lock,
                    progress_interval,
                );
            });

            result_slots
                .into_iter()
                .map(|slot| slot.lock().unwrap_or_else(|e| e.into_inner()).take().unwrap())
                .collect()
        })
    });

    Ok(results)
}

pub fn register_entropy_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<DirEntropyResult>()?;
    m.add_function(wrap_pyfunction!(probe_directories_parallel, m)?)?;
    Ok(())
}