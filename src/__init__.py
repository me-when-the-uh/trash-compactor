"""Lightweight package exports for the src namespace.

This module intentionally avoids importing heavy submodules at import time.
GUI startup imports src.* modules early, so eager imports here increase launch
latency even before any compression work begins.
"""

from importlib import import_module

__all__ = [
	"compress_directory",
	"entropy_dry_run",
	"execute_compression_plan_wrapper",
	"set_worker_cap",
	"CompressionStats",
	"ProgressTimer",
	"print_compression_summary",
	"print_entropy_dry_run",
	"PerformanceMonitor",
	"TimingStats",
]


def __getattr__(name: str):
	if name in {
		"compress_directory",
		"entropy_dry_run",
		"execute_compression_plan_wrapper",
		"set_worker_cap",
	}:
		module = import_module(".compression_module", __name__)
		return getattr(module, name)

	if name in {
		"CompressionStats",
		"ProgressTimer",
		"print_compression_summary",
		"print_entropy_dry_run",
	}:
		module = import_module(".stats", __name__)
		return getattr(module, name)

	if name in {"PerformanceMonitor", "TimingStats"}:
		module = import_module(".timer", __name__)
		return getattr(module, name)

	raise AttributeError(f"module '{__name__}' has no attribute '{name}'")