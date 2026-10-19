mod entropy;

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::IntoPyObject;
use rayon::ThreadPoolBuilder;
use std::collections::{HashSet, VecDeque};
use std::ffi::OsStr;
use std::path::{Path, PathBuf, MAIN_SEPARATOR, MAIN_SEPARATOR_STR};
use std::io::Read;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;

const BATCH_SIZE: usize = 4096;
// Workers park on send once this many batches are queued. A slow Python
// consumer bounds walker memory instead of buffering the whole tree.
const CHANNEL_DEPTH: usize = 8;

const CAT_ELIGIBLE: u8 = 0;
const CAT_EXTENSION: u8 = 1;
const CAT_TOO_SMALL: u8 = 2;
const CAT_DEBUG_EXT: u8 = 3;
const CAT_ALREADY_COMPRESSED: u8 = 4;
const CAT_ERROR: u8 = 5;
const CAT_MAGIC: u8 = 6;
const CAT_CLOUD: u8 = 7;
const CAT_SPARSE: u8 = 8;
const CAT_LINK: u8 = 9;

const SQLITE_HEADER: &[u8; 16] = b"SQLite format 3\0";

pub(crate) const FILE_ATTRIBUTE_COMPRESSED: u32 = 0x800;
pub(crate) const FILE_ATTRIBUTE_SPARSE_FILE: u32 = 0x200;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x00000400;
const FILE_ATTRIBUTE_RECALL_ON_OPEN: u32 = 0x00040000;
const FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: u32 = 0x00400000;

pub(crate) fn is_cloud_placeholder(attrs: u32) -> bool {
    attrs & (FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS) != 0
}

pub(crate) fn is_sparse(attrs: u32) -> bool {
    attrs & FILE_ATTRIBUTE_SPARSE_FILE != 0
}

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
    // The compressed-size syscall is only meaningful for categories that can
    // still be compressed. Skip categories report the logical size.
    if category != CAT_ELIGIBLE && category != CAT_DEBUG_EXT {
        return (algo, category, logical);
    }

    let physical = get_ntfs_compressed_size(path).unwrap_or(logical);

    // Sparse files are left alone: their holes are free by design, and
    // compacting one can allocate the unallocated ranges.
    if is_sparse(attrs) && attrs & FILE_ATTRIBUTE_COMPRESSED == 0 {
        return (algo, CAT_SPARSE, physical);
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
        let lower = normalized.to_lowercase();
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
pub(crate) fn file_attributes(metadata: &std::fs::Metadata) -> u32 {
    use std::os::windows::fs::MetadataExt;
    metadata.file_attributes()
}

#[cfg(not(windows))]
pub(crate) fn file_attributes(_metadata: &std::fs::Metadata) -> u32 {
    0
}

pub(crate) fn skip_extension_name(name: &OsStr, skip_ext: &HashSet<String>) -> bool {
    Path::new(name).extension().map_or(false, |ext| {
        skip_ext.contains(&*ext.to_string_lossy().to_lowercase())
    })
}

fn is_sqlite_sidecar(name: &OsStr) -> bool {
    let lower = name.to_string_lossy().to_ascii_lowercase();
    lower.ends_with("-wal") || lower.ends_with("-shm") || lower.ends_with("-journal")
}

fn sqlite_header_match(path: &Path) -> bool {
    let mut file = match std::fs::File::open(path) {
        Ok(file) => file,
        Err(_) => return false,
    };
    let mut buf = [0u8; 16];
    match file.read_exact(&mut buf) {
        Ok(()) => &buf == SQLITE_HEADER,
        Err(_) => false,
    }
}

fn looks_like_sqlite(path: &Path, name: &OsStr, size: u64, min_size: u64) -> bool {
    if is_sqlite_sidecar(name) {
        return true;
    }
    if Path::new(name).extension().is_some() {
        return false;
    }
    if size < min_size.max(16) {
        return false;
    }
    sqlite_header_match(path)
}

/// (algorithm byte, category byte). The algorithm index is shared with the
/// Python size thresholds: 0=XPRESS4K, 1=XPRESS8K, 2=XPRESS16K, 3=LZX.
fn classify(
    size: u64,
    is_skip_ext: bool,
    is_magic: bool,
    ignore_ext: bool,
    min_size: u64,
    breaks: &[u64],
) -> (u8, u8) {
    let algo = breaks.iter().filter(|&&b| size >= b).count() as u8;
    if !ignore_ext && is_skip_ext {
        return (algo, CAT_EXTENSION);
    }
    if is_magic {
        return (algo, CAT_MAGIC);
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
    cancelled: &AtomicBool,
) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        out.push((dir.to_string_lossy().into_owned(), 0, 0, 0, CAT_ERROR, 0));
        return;
    };

    let mut entry_path = dir.to_path_buf();
    let mut seen = 0usize;

    for entry in entries.flatten() {
        seen += 1;
        if seen % 64 == 0 && cancelled.load(Ordering::Acquire) {
            return;
        }
        let file_name = entry.file_name();
        entry_path.push(&file_name);

        let metadata = match entry.metadata() {
            Ok(metadata) => metadata,
            Err(_) => {
                out.push((
                    entry_path.to_string_lossy().into_owned(),
                    0,
                    0,
                    0,
                    CAT_ERROR,
                    0,
                ));
                entry_path.pop();
                continue;
            }
        };

        if metadata.is_dir() {
            if !exclusions.contains_path(entry_path.to_string_lossy().as_ref()) {
                subdirs.push(entry_path.clone());
            }
            entry_path.pop();
            continue;
        }

        let attrs = file_attributes(&metadata);
        if !metadata.is_file() {
            // Neither a directory nor a file: a junction or symlink. It is not
            // followed, and the planner reports it as a skip.
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                out.push((
                    entry_path.to_string_lossy().into_owned(),
                    0,
                    attrs,
                    0,
                    CAT_LINK,
                    0,
                ));
            }
            entry_path.pop();
            continue;
        }

        let size = metadata.len();
        if exclusions.contains_path(entry_path.to_string_lossy().as_ref()) {
            entry_path.pop();
            continue;
        }
        if is_cloud_placeholder(attrs) {
            // Opening the file would download a OneDrive placeholder.
            let algo = breaks.iter().filter(|&&b| size >= b).count() as u8;
            out.push((
                entry_path.to_string_lossy().into_owned(),
                size,
                attrs,
                algo,
                CAT_CLOUD,
                size,
            ));
            entry_path.pop();
            continue;
        }
        let is_skip_ext = skip_extension_name(&file_name, skip_ext);
        let is_magic = looks_like_sqlite(&entry_path, &file_name, size, min_size);
        let (algo, category) = classify(size, is_skip_ext, is_magic, ignore_ext, min_size, breaks);
        let path_str = entry_path.to_string_lossy().into_owned();
        let (algo, category, hint) = finalize_entry(&path_str, size, attrs, algo, category);
        out.push((path_str, size, attrs, algo, category, hint));
        entry_path.pop();
    }
}

struct SharedStack {
    dirs: Mutex<VecDeque<PathBuf>>,
    idle: Condvar,
}

fn next_dir(
    stack: &SharedStack,
    pending: &AtomicUsize,
    cancelled: &AtomicBool,
    fifo: bool,
) -> (Option<PathBuf>, bool) {
    let mut dirs = stack.dirs.lock().unwrap_or_else(|e| e.into_inner());
    loop {
        if cancelled.load(Ordering::Acquire) {
            dirs.clear();
            return (None, false);
        }
        // With a single worker (fifo=true, HDD mode) pop from the front. The
        // disk head then sweeps the tree in discovery order instead of jumping
        // between sibling branches on every pop.
        let popped = if fifo {
            dirs.pop_front()
        } else {
            dirs.pop_back()
        };
        if popped.is_some() {
            return (popped, false);
        }
        if cancelled.load(Ordering::Acquire) {
            return (None, false);
        }
        if pending.load(Ordering::Acquire) == 0 {
            return (None, true);
        }
        dirs = stack.idle.wait(dirs).unwrap_or_else(|e| e.into_inner());
    }
}

fn worker_loop(
    stack: Arc<SharedStack>,
    pending: Arc<AtomicUsize>,
    exclusions: Arc<ExclusionIndex>,
    skip_ext: Arc<HashSet<String>>,
    breaks: Arc<Vec<u64>>,
    min_size: u64,
    ignore_ext: bool,
    fifo: bool,
    tx: Arc<SyncSender<Vec<ScanTuple>>>,
    cancelled: Arc<AtomicBool>,
) {
    let mut local: Vec<ScanTuple> = Vec::with_capacity(BATCH_SIZE);

    loop {
        let (dir, done) = next_dir(&stack, &pending, &cancelled, fifo);
        let Some(dir) = dir else {
            if done && !local.is_empty() {
                let _ = tx.send(std::mem::take(&mut local));
            }
            let _guard = stack.dirs.lock().unwrap_or_else(|e| e.into_inner());
            stack.idle.notify_all();
            return;
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
                &cancelled,
            );
            subdirs
        }));

        match result {
            Ok(mut subdirs) => {
                if local.len() >= BATCH_SIZE {
                    let _ = tx.send(std::mem::take(&mut local));
                }

                if cancelled.load(Ordering::Acquire) {
                    subdirs.clear();
                }
                if !subdirs.is_empty() {
                    let n = subdirs.len();
                    {
                        let mut dirs = stack.dirs.lock().unwrap_or_else(|e| e.into_inner());
                        dirs.extend(subdirs);
                        stack.idle.notify_all();
                    }
                    pending.fetch_add(n, Ordering::Release);
                }
            }
            Err(_) => {
                // Swallow the panic. Other workers still process the
                // directories left on the stack.
            }
        }
        pending.fetch_sub(1, Ordering::Release);
    }
}

#[pyclass(unsendable)]
struct WalkIter {
    receiver: Option<Receiver<Vec<ScanTuple>>>,
    cancelled: Arc<AtomicBool>,
    stack: Arc<SharedStack>,
}

impl Drop for WalkIter {
    fn drop(&mut self) {
        self.cancelled.store(true, Ordering::Release);
        // Parked workers re-check `cancelled` under the stack lock. The wake
        // has to happen while that lock is held.
        {
            let _guard = self.stack.dirs.lock().unwrap_or_else(|e| e.into_inner());
            self.stack.idle.notify_all();
        }
        if let Some(receiver) = self.receiver.as_ref() {
            while receiver.try_recv().is_ok() {}
        }
    }
}

#[pymethods]
impl WalkIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<'_, Self>) -> PyResult<Option<Py<PyAny>>> {
        let py = slf.py();
        let Some(receiver) = slf.receiver.take() else {
            return Ok(None);
        };
        // Waiting without the GIL keeps the GUI thread responsive while the
        // walkers fill the next batch. The receiver moves through the closure
        // because a shared reference would not be Send.
        let (batch, receiver) = py.detach(move || {
            let batch = receiver.recv().ok();
            (batch, receiver)
        });
        slf.receiver = Some(receiver);
        let Some(batch) = batch else {
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
    let (tx, rx) = sync_channel(CHANNEL_DEPTH);
    let skip_ext: HashSet<String> = skip_extensions
        .into_iter()
        .map(|e| e.strip_prefix('.').unwrap_or(&e).to_lowercase())
        .collect();
    let exclusions = Arc::new(ExclusionIndex::new(excluded_dirs));
    let skip_ext = Arc::new(skip_ext);
    let breaks = Arc::new(size_breaks);
    let threads = workers.max(1);
    let fifo = threads == 1;
    let cancelled = Arc::new(AtomicBool::new(false));
    let cancelled_thread = Arc::clone(&cancelled);
    let stack = Arc::new(SharedStack {
        dirs: Mutex::new(VecDeque::from([PathBuf::from(root)])),
        idle: Condvar::new(),
    });
    let pending = Arc::new(AtomicUsize::new(1));

    let pool = ThreadPoolBuilder::new()
        .num_threads(threads)
        .build()
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

    let stack_thread = Arc::clone(&stack);
    thread::spawn(move || {
        let tx = Arc::new(tx);
        pool.scope(|s| {
            for _ in 0..threads {
                let tx = Arc::clone(&tx);
                let stack = Arc::clone(&stack_thread);
                let pending = Arc::clone(&pending);
                let exclusions = Arc::clone(&exclusions);
                let skip_ext = Arc::clone(&skip_ext);
                let breaks = Arc::clone(&breaks);
                let cancelled = Arc::clone(&cancelled_thread);
                s.spawn(move |_| {
                    worker_loop(stack, pending, exclusions, skip_ext, breaks, min_size, ignore_extensions, fifo, tx, cancelled);
                });
            }
        });
    });

    Ok(WalkIter {
        receiver: Some(rx),
        cancelled,
        stack,
    })
}

#[pymodule]
fn fast_walk(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WalkIter>()?;
    m.add_function(wrap_pyfunction!(walk_and_filter, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    entropy::register_entropy_module(m)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_for_compare_folds_cyrillic() {
        let folded = normalize_for_compare("D:/Папка/ФАЙЛ");
        assert!(folded.contains('\\'));
        #[cfg(windows)]
        {
            assert_eq!(folded, normalize_for_compare(r"D:\папка\файл"));
            assert!(folded.contains('п'));
            assert!(folded.contains('ф'));
        }
    }

    #[test]
    fn cloud_placeholder_attributes() {
        assert!(is_cloud_placeholder(FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS));
        assert!(is_cloud_placeholder(FILE_ATTRIBUTE_RECALL_ON_OPEN));
        assert!(is_cloud_placeholder(
            FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_COMPRESSED
        ));
        assert!(!is_cloud_placeholder(FILE_ATTRIBUTE_COMPRESSED));
        assert!(!is_cloud_placeholder(0));
    }

    #[test]
    fn sparse_entries_skip_the_compression_checks() {
        assert!(is_sparse(FILE_ATTRIBUTE_SPARSE_FILE));
        assert!(!is_sparse(FILE_ATTRIBUTE_COMPRESSED));
        // The path does not exist: the physical size falls back to the logical
        // one, and the attributes alone decide the category.
        let (_, category, _) = finalize_entry("Z:\\missing", 100, FILE_ATTRIBUTE_SPARSE_FILE, 0, CAT_ELIGIBLE);
        assert_eq!(category, CAT_SPARSE);
        let (_, category, _) = finalize_entry(
            "Z:\\missing",
            100,
            FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED,
            0,
            CAT_ELIGIBLE,
        );
        assert_eq!(category, CAT_ALREADY_COMPRESSED);
    }

    #[test]
    fn next_dir_drops_the_queue_when_cancelled() {
        let stack = SharedStack {
            dirs: Mutex::new(VecDeque::from([
                PathBuf::from(r"C:\a"),
                PathBuf::from(r"C:\b"),
            ])),
            idle: Condvar::new(),
        };
        let pending = AtomicUsize::new(2);
        let cancelled = AtomicBool::new(true);
        let (dir, done) = next_dir(&stack, &pending, &cancelled, true);
        assert!(dir.is_none());
        assert!(!done);
        assert!(stack.dirs.lock().unwrap_or_else(|e| e.into_inner()).is_empty());
    }
}
