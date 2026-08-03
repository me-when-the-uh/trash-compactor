mod entropy;

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::IntoPyObject;
use rayon::ThreadPoolBuilder;
use std::collections::HashSet;
use std::ffi::OsStr;
use std::path::{Path, PathBuf, MAIN_SEPARATOR, MAIN_SEPARATOR_STR};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;

const BATCH_SIZE: usize = 4096;

const CAT_ELIGIBLE: u8 = 0;
const CAT_EXTENSION: u8 = 1;
const CAT_TOO_SMALL: u8 = 2;
const CAT_DEBUG_EXT: u8 = 3;
const CAT_ALREADY_COMPRESSED: u8 = 4;

const FILE_ATTRIBUTE_COMPRESSED: u32 = 0x800;

type ScanTuple = (String, u64, u32, u8, u8, u64);

#[cfg(windows)]
fn get_ntfs_compressed_size(path: &str) -> Result<u64, ()> {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{GetLastError, WIN32_ERROR};
    use windows::Win32::Storage::FileSystem::GetCompressedFileSizeW;

    let wide: Vec<u16> = OsStr::new(path)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut high = 0u32;
    let low = unsafe { GetCompressedFileSizeW(PCWSTR(wide.as_ptr()), Some(&mut high)) };
    if low == 0xFFFFFFFF {
        if unsafe { GetLastError() } != WIN32_ERROR(0) {
            return Err(());
        }
        return Ok((high as u64) << 32 | low as u64);
    }
    Ok((high as u64) << 32 | low as u64)
}

#[cfg(not(windows))]
fn get_ntfs_compressed_size(_path: &str) -> Result<u64, ()> {
    Err(())
}

fn finalize_entry(
    path: &str,
    logical: u64,
    attrs: u32,
    algo: u8,
    category: u8,
) -> (u8, u8, u64) {
    let physical = get_ntfs_compressed_size(path).unwrap_or(logical);

    if category != CAT_ELIGIBLE && category != CAT_DEBUG_EXT {
        return (algo, category, physical);
    }

    if physical < logical || (attrs & FILE_ATTRIBUTE_COMPRESSED != 0) {
        return (algo, CAT_ALREADY_COMPRESSED, physical);
    }

    (algo, category, physical)
}

fn normalize_for_compare(path: &str) -> String {
    let normalized = path.replace('/', MAIN_SEPARATOR_STR);
    #[cfg(windows)]
    {
        let lower = normalized.to_ascii_lowercase();
        if lower.len() == 2 && lower.as_bytes().get(1) == Some(&b':') {
            return format!("{lower}{MAIN_SEPARATOR}");
        }
        lower
    }
    #[cfg(not(windows))]
    {
        normalized
    }
}

struct ExclusionIndex {
    exact: HashSet<String>,
    prefixes: Vec<String>,
    dotted: Vec<String>,
}

impl ExclusionIndex {
    fn new(excluded_dirs: Vec<String>) -> Self {
        let mut exact = HashSet::with_capacity(excluded_dirs.len());
        let mut prefixes = Vec::with_capacity(excluded_dirs.len());
        let mut dotted = Vec::new();

        for dir in excluded_dirs {
            let norm = normalize_for_compare(&dir);
            exact.insert(norm.clone());
            prefixes.push(format!("{norm}{MAIN_SEPARATOR}"));
            // Windows.old. entries form a dotted namespace: any name starting
            // with "<root>\windows.old." is a stale Windows install.
            if norm.ends_with(&format!("{MAIN_SEPARATOR_STR}windows.old.")) {
                dotted.push(norm);
            }
        }

        Self { exact, prefixes, dotted }
    }

    fn contains_path(&self, path: &str) -> bool {
        let norm = normalize_for_compare(path);
        self.exact.contains(&norm)
            || self.prefixes.iter().any(|prefix| norm.starts_with(prefix))
            || self.dotted.iter().any(|prefix| norm.starts_with(prefix))
    }
}

#[cfg(windows)]
fn file_attributes(metadata: &std::fs::Metadata) -> u32 {
    use std::os::windows::fs::MetadataExt;
    metadata.file_attributes()
}

#[cfg(not(windows))]
fn file_attributes(_metadata: &std::fs::Metadata) -> u32 {
    0
}

fn skip_extension_name(name: &OsStr, skip_ext: &HashSet<String>) -> bool {
    Path::new(name).extension().map_or(false, |ext| {
        skip_ext.contains(&*ext.to_string_lossy().to_lowercase())
    })
}

/// (algorithm byte, category byte). Algorithm index matches SIZE_THRESHOLDS
/// ordering in Python: 0=XPRESS4K, 1=XPRESS8K, 2=XPRESS16K, 3=LZX.
fn classify(size: u64, is_skip_ext: bool, ignore_ext: bool, min_size: u64, breaks: &[u64]) -> (u8, u8) {
    let algo = breaks.iter().filter(|&&b| size >= b).count() as u8;
    if !ignore_ext && is_skip_ext {
        return (algo, CAT_EXTENSION);
    }
    if size < min_size {
        return (algo, CAT_TOO_SMALL);
    }
    if ignore_ext && is_skip_ext {
        return (algo, CAT_DEBUG_EXT);
    }
    (algo, CAT_ELIGIBLE)
}

fn scan_dir(
    dir: &Path,
    exclusions: &ExclusionIndex,
    skip_ext: &HashSet<String>,
    ignore_ext: bool,
    min_size: u64,
    breaks: &[u64],
    out: &mut Vec<ScanTuple>,
    subdirs: &mut Vec<PathBuf>,
) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };

    let mut entry_path = dir.to_path_buf();

    for entry in entries.flatten() {
        let Ok(metadata) = entry.metadata() else {
            continue;
        };

        let file_name = entry.file_name();
        entry_path.push(&file_name);

        if metadata.is_dir() {
            if !exclusions.contains_path(entry_path.to_string_lossy().as_ref()) {
                subdirs.push(entry_path.clone());
            }
            entry_path.pop();
            continue;
        }

        if !metadata.is_file() {
            entry_path.pop();
            continue;
        }

        let size = metadata.len();
        let is_skip_ext = skip_extension_name(&file_name, skip_ext);
        let (algo, category) = classify(size, is_skip_ext, ignore_ext, min_size, breaks);
        let path_str = entry_path.to_string_lossy().into_owned();
        let attrs = file_attributes(&metadata);
        let (algo, category, hint) = finalize_entry(&path_str, size, attrs, algo, category);
        out.push((path_str, size, attrs, algo, category, hint));
        entry_path.pop();
    }
}

fn worker_loop(
    stack: Arc<Mutex<Vec<PathBuf>>>,
    pending: Arc<AtomicUsize>,
    exclusions: Arc<ExclusionIndex>,
    skip_ext: Arc<HashSet<String>>,
    breaks: Arc<Vec<u64>>,
    min_size: u64,
    ignore_ext: bool,
    fifo: bool,
    tx: Sender<Vec<ScanTuple>>,
) {
    let mut local: Vec<ScanTuple> = Vec::with_capacity(BATCH_SIZE);

    loop {
        if pending.load(Ordering::Acquire) == 0 {
            if !local.is_empty() {
                let _ = tx.send(std::mem::take(&mut local));
            }
            return;
        }

        // With a single worker (fifo=true, HDD mode) pop from the front so the
        // disk head sweeps the tree in discovery order instead of jumping
        // between sibling branches on every pop.
        let dir = {
            let mut stack = stack.lock().unwrap_or_else(|e| e.into_inner());
            if fifo {
                if stack.is_empty() {
                    None
                } else {
                    Some(stack.remove(0))
                }
            } else {
                stack.pop()
            }
        };
        let Some(dir) = dir else {
            std::thread::yield_now();
            continue;
        };

        // A panic inside scan_dir (e.g. a poisoned lock) must not kill the
        // walk: catch it, still decrement pending, and keep going. Dropping
        // this worker would leave pending > 0 forever and hang the iterator.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut subdirs: Vec<PathBuf> = Vec::new();
            scan_dir(
                &dir,
                &exclusions,
                &skip_ext,
                ignore_ext,
                min_size,
                &breaks,
                &mut local,
                &mut subdirs,
            );
            subdirs
        }));

        match result {
            Ok(subdirs) => {
                if local.len() >= BATCH_SIZE {
                    let _ = tx.send(std::mem::take(&mut local));
                }

                if !subdirs.is_empty() {
                    let n = subdirs.len();
                    stack.lock().unwrap_or_else(|e| e.into_inner()).extend(subdirs);
                    pending.fetch_add(n, Ordering::Release);
                }
            }
            Err(_) => {
                // Swallow the panic; remaining directories on the stack are
                // still processed by other workers.
            }
        }
        pending.fetch_sub(1, Ordering::Release);
    }
}

#[pyclass(unsendable)]
struct WalkIter {
    receiver: Receiver<Vec<ScanTuple>>,
}

#[pymethods]
impl WalkIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(slf: PyRefMut<'_, Self>) -> PyResult<Option<Py<PyAny>>> {
        let py = slf.py();
        let Some(batch) = slf.receiver.recv().ok() else {
            return Ok(None);
        };

        let py_list = PyList::empty(py);
        for (path, size, attrs, algo, category, hint) in batch {
            py_list.append((path, size, attrs, algo, category, hint).into_pyobject(py)?)?;
        }
        Ok(Some(py_list.into()))
    }
}

#[pyfunction]
fn walk_and_filter(
    root: String,
    excluded_dirs: Vec<String>,
    skip_extensions: Vec<String>,
    min_size: u64,
    size_breaks: Vec<u64>,
    ignore_extensions: bool,
    workers: usize,
) -> PyResult<WalkIter> {
    let (tx, rx) = channel();
    let skip_ext: HashSet<String> = skip_extensions
        .into_iter()
        .map(|e| e.strip_prefix('.').unwrap_or(&e).to_lowercase())
        .collect();
    let exclusions = Arc::new(ExclusionIndex::new(excluded_dirs));
    let skip_ext = Arc::new(skip_ext);
    let breaks = Arc::new(size_breaks);
    let threads = workers.max(1);
    let fifo = threads == 1;

    thread::spawn(move || {
        let Ok(pool) = ThreadPoolBuilder::new().num_threads(threads).build() else {
            return;
        };

        let stack = Arc::new(Mutex::new(vec![PathBuf::from(root)]));
        let pending = Arc::new(AtomicUsize::new(1));

        pool.scope(|s| {
            for _ in 0..threads {
                let tx = tx.clone();
                let stack = Arc::clone(&stack);
                let pending = Arc::clone(&pending);
                let exclusions = Arc::clone(&exclusions);
                let skip_ext = Arc::clone(&skip_ext);
                let breaks = Arc::clone(&breaks);
                s.spawn(move |_| {
                    worker_loop(stack, pending, exclusions, skip_ext, breaks, min_size, ignore_extensions, fifo, tx);
                });
            }
        });
    });

    Ok(WalkIter { receiver: rx })
}

#[pymodule]
fn fast_walk(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WalkIter>()?;
    m.add_function(wrap_pyfunction!(walk_and_filter, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    entropy::register_entropy_module(m)?;
    Ok(())
}
