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

#[pyclass]
#[derive(Clone)]
pub struct EntropyParams {
    #[pyo3(get)]
    pub dynamic_windows_min_file_size: u64,
    #[pyo3(get)]
    pub dynamic_windows_max_file_size: u64,
    #[pyo3(get)]
    pub huge_windows_file_size: u64,
    #[pyo3(get)]
    pub base_sample_windows: u32,
    #[pyo3(get)]
    pub dynamic_windows_min: u32,
    #[pyo3(get)]
    pub dynamic_windows_max: u32,
    #[pyo3(get)]
    pub huge_windows_max: u32,
    #[pyo3(get)]
    pub target_window_size: u64,
}

#[pymethods]
impl EntropyParams {
    #[new]
    #[pyo3(signature = (dynamic_windows_min_file_size, dynamic_windows_max_file_size, huge_windows_file_size, base_sample_windows, dynamic_windows_min, dynamic_windows_max, huge_windows_max, target_window_size))]
    fn new(
        dynamic_windows_min_file_size: u64,
        dynamic_windows_max_file_size: u64,
        huge_windows_file_size: u64,
        base_sample_windows: u32,
        dynamic_windows_min: u32,
        dynamic_windows_max: u32,
        huge_windows_max: u32,
        target_window_size: u64,
    ) -> Self {
        EntropyParams {
            dynamic_windows_min_file_size,
            dynamic_windows_max_file_size,
            huge_windows_file_size,
            base_sample_windows,
            dynamic_windows_min,
            dynamic_windows_max,
            huge_windows_max,
            target_window_size,
        }
    }
}

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
    #[pyo3(get)]
    pub has_lz4_certain: bool,
    #[pyo3(get)]
    pub sampled_paths: Vec<String>,
    #[pyo3(get)]
    pub lz4_certain_paths: Vec<String>,
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
    path: String,
    file_size: u64,
    weighted_entropy: f64,
    sampled_bytes: u64,
    lz4_certain: bool,
}

/// Bounded largest-N selection merged into the walk: no full per-directory
/// file list is ever materialised.
struct SampleSelection {
    max_files: usize,
    entries: Vec<(u64, String)>,
}

impl SampleSelection {
    fn new(max_files: u32) -> Self {
        SampleSelection {
            max_files: max_files as usize,
            entries: Vec::with_capacity((max_files as usize).min(1024)),
        }
    }

    fn consider(&mut self, path: String, size: u64) {
        if self.max_files == 0 {
            return;
        }
        if self.entries.len() < self.max_files {
            self.entries.push((size, path));
            if self.entries.len() == self.max_files {
                self.entries.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
            }
            return;
        }
        // The tail is the current smallest candidate.
        let (smallest_size, _) = self.entries[self.entries.len() - 1];
        if size <= smallest_size {
            return;
        }
        let pos = self
            .entries
            .binary_search_by(|(s, p)| size.cmp(s).then_with(|| path.as_str().cmp(p.as_str())))
            .unwrap_or_else(|p| p);
        self.entries.insert(pos, (size, path));
        self.entries.pop();
    }

    /// Fold another selection's candidates into this one (parallel workers).
    fn merge(&mut self, other: SampleSelection) {
        for (size, path) in other.entries {
            self.consider(path, size);
        }
    }

    /// Top ``strata`` slots for the deterministic largest files; the rest is a
    /// regular stride over the remainder. Mirrors the old full-list behaviour.
    fn finalize(mut self) -> Vec<(String, u64)> {
        if self.entries.len() <= self.max_files {
            return self
                .entries
                .into_iter()
                .map(|(size, path)| (path, size))
                .collect();
        }
        let strata = (self.max_files / 5).max(1);
        let top_k = self.max_files.saturating_sub(strata);
        let mut selected: Vec<(String, u64)> = Vec::with_capacity(self.max_files);
        for (size, path) in self.entries.drain(..top_k) {
            selected.push((path, size));
        }
        if strata > 0 && !self.entries.is_empty() {
            let step = self.entries.len() as f64 / strata as f64;
            for j in 0..strata {
                let idx = (j as f64 * step) as usize;
                let (size, path) = &self.entries[idx];
                selected.push((path.clone(), *size));
            }
        }
        selected
    }
}

/// Walk ``root`` once, feeding files into ``selection``; returns subdirs so
/// the caller can keep walking.
fn scan_dir_into_selection(
    dir: &Path,
    selection: &mut SampleSelection,
    subdirs: &mut Vec<PathBuf>,
) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let Ok(metadata) = entry.metadata() else {
            continue;
        };
        if metadata.is_dir() {
            subdirs.push(entry.path());
            continue;
        }
        if !metadata.is_file() {
            continue;
        }
        let size = metadata.len();
        if size == 0 {
            continue;
        }
        selection.consider(entry.path().to_string_lossy().into_owned(), size);
    }
}

fn collect_sample_selection(
    root: &Path,
    max_files: u32,
    include_subdirectories: bool,
    breadth_first: bool,
) -> Vec<(String, u64)> {
    let mut selection = SampleSelection::new(max_files);
    let mut pending: Vec<PathBuf> = vec![root.to_path_buf()];
    let mut head = 0usize;

    while head < pending.len() {
        let dir = if breadth_first {
            let d = pending[head].clone();
            head += 1;
            d
        } else {
            pending.pop().unwrap()
        };

        let mut subdirs: Vec<PathBuf> = Vec::new();
        scan_dir_into_selection(&dir, &mut selection, &mut subdirs);
        if include_subdirectories {
            pending.extend(subdirs);
        }
    }

    selection.finalize()
}

fn collect_sample_selection_parallel(root: &Path, max_files: u32) -> Vec<(String, u64)> {
    let stack: Arc<Mutex<Vec<PathBuf>>> = Arc::new(Mutex::new(vec![root.to_path_buf()]));
    let pending = Arc::new(AtomicUsize::new(1));
    let shared: Arc<Mutex<SampleSelection>> = Arc::new(Mutex::new(SampleSelection::new(max_files)));

    rayon::scope(|s| {
        for _ in 0..rayon::current_num_threads().max(1) {
            let stack = Arc::clone(&stack);
            let pending = Arc::clone(&pending);
            let shared = Arc::clone(&shared);
            s.spawn(move |_| {
                let mut local = SampleSelection::new(max_files);
                loop {
                    if pending.load(Ordering::Acquire) == 0 {
                        shared
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .merge(local);
                        return;
                    }

                    let dir = {
                        let mut stack = stack.lock().unwrap_or_else(|e| e.into_inner());
                        stack.pop()
                    };
                    let Some(dir) = dir else {
                        std::thread::yield_now();
                        continue;
                    };

                    let Ok(entries) = std::fs::read_dir(&dir) else {
                        pending.fetch_sub(1, Ordering::AcqRel);
                        continue;
                    };

                    let mut new_dirs: Vec<PathBuf> = Vec::new();
                    for entry in entries.flatten() {
                        let Ok(metadata) = entry.metadata() else {
                            continue;
                        };
                        if metadata.is_dir() {
                            new_dirs.push(entry.path());
                            continue;
                        }
                        if !metadata.is_file() {
                            continue;
                        }
                        let size = metadata.len();
                        if size == 0 {
                            continue;
                        }
                        local.consider(entry.path().to_string_lossy().into_owned(), size);
                    }

                    if !new_dirs.is_empty() {
                        pending.fetch_add(new_dirs.len(), Ordering::AcqRel);
                        stack.lock().unwrap_or_else(|e| e.into_inner()).extend(new_dirs);
                    }
                    pending.fetch_sub(1, Ordering::AcqRel);
                }
            });
        }
    });

    let mut selection = shared.lock().unwrap_or_else(|e| e.into_inner());
    let selection = std::mem::replace(&mut *selection, SampleSelection::new(0));
    selection.finalize()
}

fn get_sample_window_count(params: &EntropyParams, file_size: u64) -> u32 {
    if file_size <= params.dynamic_windows_min_file_size {
        return params.base_sample_windows;
    }
    if file_size >= params.huge_windows_file_size {
        return params.huge_windows_max;
    }

    if file_size >= params.dynamic_windows_max_file_size {
        let ratio = (file_size - params.dynamic_windows_max_file_size) as f64
            / (params.huge_windows_file_size - params.dynamic_windows_max_file_size) as f64;
        return params.dynamic_windows_max
            + (ratio * (params.huge_windows_max - params.dynamic_windows_max) as f64) as u32;
    }

    let ratio = (file_size - params.dynamic_windows_min_file_size) as f64
        / (params.dynamic_windows_max_file_size - params.dynamic_windows_min_file_size) as f64;
    params.dynamic_windows_min
        + (ratio * (params.dynamic_windows_max - params.dynamic_windows_min) as f64) as u32
}

fn get_file_probe_budget(params: &EntropyParams, file_size: u64) -> u64 {
    get_sample_window_count(params, file_size) as u64 * params.target_window_size
}

fn derive_window_size(params: &EntropyParams, byte_budget: u64, num_windows: u32) -> u64 {
    if byte_budget == 0 {
        return 0;
    }
    let mut window = params.target_window_size.min(byte_budget);
    if window * num_windows as u64 > byte_budget && byte_budget >= num_windows as u64 {
        window = byte_budget / num_windows as u64;
    }
    window.max(1)
}

fn plan_sample_windows(
    params: &EntropyParams,
    file_size: u64,
    window_size: u64,
    num_windows: u32,
) -> Vec<(u64, u64)> {
    if file_size == 0 || window_size == 0 || num_windows == 0 {
        return Vec::new();
    }

    if file_size <= window_size {
        return vec![(0, file_size)];
    }

    let mut raw_windows: Vec<(u64, u64)> = Vec::new();

    if num_windows == 3 && params.base_sample_windows == 3 {
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

fn probe_file(params: &EntropyParams, path: &str, file_size: u64, byte_budget: u64) -> (f64, u64, bool) {
    if byte_budget == 0 || file_size == 0 {
        return (0.0, 0, false);
    }

    // A panic (e.g. an lz4/mmap edge case) must not poison the rayon batch:
    // degrade to "no sample" so the directory is just skipped by the entropy
    // gate, exactly like a file that failed to open.
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| probe_file_inner(params, path, file_size, byte_budget)))
        .unwrap_or((0.0, 0, false))
}

fn probe_file_inner(params: &EntropyParams, path: &str, file_size: u64, byte_budget: u64) -> (f64, u64, bool) {
    let num_windows = get_sample_window_count(params, file_size);
    let window_size = derive_window_size(params, byte_budget, num_windows);
    let windows = plan_sample_windows(params, file_size, window_size, num_windows);
    if windows.is_empty() {
        return (0.0, 0, false);
    }

    let mut file = File::open(path).ok();
    let mmap = if file_size >= params.dynamic_windows_min_file_size {
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

fn build_probe_jobs(
    params: &EntropyParams,
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
        let per_file = get_file_probe_budget(params, *size);
        let budget = per_file.min(chunk_size).min(remaining);
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
    params: &EntropyParams,
    dir: &str,
    max_files: u32,
    chunk_size: u64,
    max_bytes: u64,
    include_subdirectories: bool,
    breadth_first: bool,
) -> DirPlan {
    let root = Path::new(dir);
    let sampled_list = if !breadth_first && include_subdirectories {
        collect_sample_selection_parallel(root, max_files)
    } else {
        collect_sample_selection(root, max_files, include_subdirectories, breadth_first)
    };
    let jobs = build_probe_jobs(params, &sampled_list, chunk_size, max_bytes);
    DirPlan {
        dir: dir.to_string(),
        jobs,
    }
}

fn aggregate_directory(
    outcomes: &mut [FileProbeOutcome],
    max_bytes: u64,
    collect_paths: bool,
) -> (f64, u32, u64, u32, bool, Vec<String>, Vec<String>) {
    outcomes.sort_unstable_by_key(|o| o.order);

    let mut sampled_files = 0u32;
    let mut sampled_bytes = 0u64;
    let mut size_weighted_entropy = 0.0f64;
    let mut size_total = 0u64;
    let mut lz4_certain_files = 0u32;
    let mut has_lz4_certain = false;
    let mut sampled_paths: Vec<String> = Vec::new();
    let mut lz4_certain_paths: Vec<String> = Vec::new();

    for outcome in outcomes.iter() {
        if sampled_bytes >= max_bytes {
            break;
        }
        if outcome.sampled_bytes == 0 {
            continue;
        }

        sampled_files += 1;
        sampled_bytes += outcome.sampled_bytes;
        size_weighted_entropy += (outcome.weighted_entropy / outcome.sampled_bytes as f64)
            * outcome.file_size as f64;
        size_total += outcome.file_size;
        if collect_paths {
            sampled_paths.push(outcome.path.clone());
        }
        if outcome.lz4_certain {
            lz4_certain_files += 1;
            has_lz4_certain = true;
            if collect_paths {
                lz4_certain_paths.push(outcome.path.clone());
            }
        }

        if sampled_bytes >= max_bytes {
            break;
        }
    }

    if size_total == 0 {
        return (
            -1.0,
            sampled_files,
            sampled_bytes,
            lz4_certain_files,
            has_lz4_certain,
            sampled_paths,
            lz4_certain_paths,
        );
    }

    let average_entropy = size_weighted_entropy / size_total as f64;
    (
        average_entropy,
        sampled_files,
        sampled_bytes,
        lz4_certain_files,
        has_lz4_certain,
        sampled_paths,
        lz4_certain_paths,
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
#[pyo3(signature = (params, dirs, max_files, max_bytes, chunk_size, include_subdirectories, workers, collect_paths, progress_callback=None))]
pub fn probe_directories_parallel(
    py: Python<'_>,
    params: EntropyParams,
    dirs: Vec<String>,
    max_files: u32,
    max_bytes: u64,
    chunk_size: u64,
    include_subdirectories: bool,
    workers: usize,
    collect_paths: bool,
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
                    &params,
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
                        probe_file(&params, &job.path, job.size, job.budget);
                    outcomes.push(FileProbeOutcome {
                        order: job.order,
                        path: job.path.clone(),
                        file_size: job.size,
                        weighted_entropy,
                        sampled_bytes,
                        lz4_certain,
                    });
                }
                let (average_entropy, sampled_files, sampled_bytes, lz4_certain, has_lz4_certain, sampled_paths, lz4_certain_paths) =
                    aggregate_directory(&mut outcomes, max_bytes, collect_paths);
                results.push(DirEntropyResult {
                    dir: plan.dir.clone(),
                    average_entropy,
                    sampled_files,
                    sampled_bytes,
                    lz4_certain,
                    has_lz4_certain,
                    sampled_paths,
                    lz4_certain_paths,
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
            let result_slots: Vec<Arc<Mutex<Option<DirEntropyResult>>>> = (0..dirs.len())
                .map(|_| Arc::new(Mutex::new(None)))
                .collect();
            let dirs_completed = Arc::new(AtomicUsize::new(0));

            dirs.par_iter().enumerate().for_each(|(dir_index, dir)| {
                let plan = prepare_directory_plan(
                    &params,
                    dir,
                    max_files,
                    chunk_size,
                    max_bytes,
                    include_subdirectories,
                    false,
                );

                let mut outcomes: Vec<FileProbeOutcome> = Vec::new();
                for job in &plan.jobs {
                    let (weighted_entropy, sampled_bytes, lz4_certain) =
                        probe_file(&params, &job.path, job.size, job.budget);
                    outcomes.push(FileProbeOutcome {
                        order: job.order,
                        path: job.path.clone(),
                        file_size: job.size,
                        weighted_entropy,
                        sampled_bytes,
                        lz4_certain,
                    });
                }

                let (average_entropy, sampled_files, sampled_bytes, lz4_certain, has_lz4_certain, sampled_paths, lz4_certain_paths) =
                    aggregate_directory(&mut outcomes, max_bytes, collect_paths);
                *result_slots[dir_index].lock().unwrap_or_else(|e| e.into_inner()) =
                    Some(DirEntropyResult {
                        dir: plan.dir.clone(),
                        average_entropy,
                        sampled_files,
                        sampled_bytes,
                        lz4_certain,
                        has_lz4_certain,
                        sampled_paths,
                        lz4_certain_paths,
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
    m.add_class::<EntropyParams>()?;
    m.add_function(wrap_pyfunction!(probe_directories_parallel, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selection_keeps_largest_files_bounded() {
        let mut sel = SampleSelection::new(10);
        for i in 0..1000u64 {
            sel.consider(format!("f{i}"), i);
        }
        assert_eq!(sel.entries.len(), 10);
        // The kept entries must be the 10 largest, descending by size.
        let sizes: Vec<u64> = sel.entries.iter().map(|(s, _)| *s).collect();
        assert_eq!(sizes, vec![999, 998, 997, 996, 995, 994, 993, 992, 991, 990]);
    }

    #[test]
    fn selection_merges_workers() {
        let mut a = SampleSelection::new(5);
        let mut b = SampleSelection::new(5);
        for i in 0..10u64 {
            if i % 2 == 0 {
                a.consider(format!("a{i}"), i);
            } else {
                b.consider(format!("b{i}"), i);
            }
        }
        a.merge(b);
        assert_eq!(a.entries.len(), 5);
        let sizes: Vec<u64> = a.entries.iter().map(|(s, _)| *s).collect();
        assert_eq!(sizes, vec![9, 8, 7, 6, 5]);
    }

    #[test]
    fn selection_finalize_matches_strata_behavior() {
        let mut sel = SampleSelection::new(10);
        for i in (0..100u64).rev() {
            sel.consider(format!("f{i}"), i);
        }
        let finalized = sel.finalize();
        assert_eq!(finalized.len(), 10);
        // Top 8 largest (90..99), then a stride sample from the remaining pool.
        let first_eight: Vec<u64> = finalized.iter().take(8).map(|(_, s)| *s).collect();
        assert_eq!(first_eight, vec![99, 98, 97, 96, 95, 94, 93, 92]);
    }
}
