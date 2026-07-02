use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::IntoPyObject;
use std::collections::HashSet;
use std::path::{Path, PathBuf, MAIN_SEPARATOR, MAIN_SEPARATOR_STR};
use std::sync::mpsc::{channel, Receiver};
use std::thread;

const BATCH_SIZE: usize = 4096;

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
}

impl ExclusionIndex {
    fn new(excluded_dirs: Vec<String>) -> Self {
        let mut exact = HashSet::with_capacity(excluded_dirs.len());
        let mut prefixes = Vec::with_capacity(excluded_dirs.len());

        for dir in excluded_dirs {
            let norm = normalize_for_compare(&dir);
            exact.insert(norm.clone());
            prefixes.push(format!("{norm}{MAIN_SEPARATOR}"));
        }

        Self { exact, prefixes }
    }

    fn contains_path(&self, path: &str) -> bool {
        let norm = normalize_for_compare(path);
        self.exact.contains(&norm)
            || self.prefixes.iter().any(|prefix| norm.starts_with(prefix))
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

fn read_dir(
    current: &Path,
    exclusions: &ExclusionIndex,
    batch: &mut Vec<(String, u64, u32)>,
    subdirs: &mut Vec<PathBuf>,
) {
    let Ok(entries) = std::fs::read_dir(current) else {
        return;
    };

    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(metadata) = entry.metadata() else {
            continue;
        };

        if metadata.is_dir() {
            let path_key = path.to_string_lossy();
            if exclusions.contains_path(path_key.as_ref()) {
                continue;
            }
            subdirs.push(path);
            continue;
        }

        if metadata.is_file() {
            let path_str = path.to_string_lossy().into_owned();
            if exclusions.contains_path(&path_str) {
                continue;
            }
            batch.push((path_str, metadata.len(), file_attributes(&metadata)));
        }
    }
}

fn collect_batch(stack: &mut Vec<PathBuf>, exclusions: &ExclusionIndex) -> Vec<(String, u64, u32)> {
    let mut batch = Vec::with_capacity(BATCH_SIZE);

    while batch.len() < BATCH_SIZE {
        let Some(current) = stack.pop() else {
            break;
        };

        let mut subdirs = Vec::new();
        read_dir(&current, exclusions, &mut batch, &mut subdirs);
        stack.extend(subdirs.into_iter().rev());
    }

    batch
}

fn walk_worker(
    root: String,
    exclusions: ExclusionIndex,
    _workers: usize,
    sender: std::sync::mpsc::Sender<Vec<(String, u64, u32)>>,
) {
    let mut stack = vec![PathBuf::from(root)];
    loop {
        let batch = collect_batch(&mut stack, &exclusions);
        if batch.is_empty() {
            break;
        }
        if sender.send(batch).is_err() {
            break;
        }
    }
}

#[pyclass(unsendable)]
struct WalkIter {
    receiver: Receiver<Vec<(String, u64, u32)>>,
}

#[pymethods]
impl WalkIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(slf: PyRefMut<'_, Self>) -> PyResult<Option<Py<PyAny>>> {
        let py = slf.py();
        let batch = slf.receiver.recv().ok();

        let Some(batch) = batch else {
            return Ok(None);
        };

        let py_list = PyList::empty(py);
        for (path, size, attrs) in batch {
            py_list.append((path, size, attrs).into_pyobject(py)?)?;
        }
        Ok(Some(py_list.into()))
    }
}

#[pyfunction]
fn walk_files(root: String, excluded_dirs: Vec<String>, workers: usize) -> PyResult<WalkIter> {
    let (sender, receiver) = channel();
    let exclusions = ExclusionIndex::new(excluded_dirs);

    thread::spawn(move || walk_worker(root, exclusions, workers, sender));

    Ok(WalkIter { receiver })
}

#[pymodule]
fn fast_walk(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WalkIter>()?;
    m.add_function(wrap_pyfunction!(walk_files, m)?)?;
    Ok(())
}