import time
from . import config
from .i18n import _

_benchmark_cached_result = None

def run_benchmark() -> bool:
    """
    Runs a CPU benchmark simulating decompression workload.
    If the machine is too slow and exceeds the configured duration limit, LZX compression will be disabled.
    We don't want to make it even slower after compressing files with LZX which only gives additional 5-10% storage savings idk.
    """
    global _benchmark_cached_result
    if _benchmark_cached_result is not None:
        return _benchmark_cached_result

    start_time = time.perf_counter()
    deadline = start_time + config.BENCHMARK_DURATION_LIMIT
    iterations = config.BENCHMARK_WORKLOAD_ITERATIONS
    
    # Sliding window simulation (64KB)
    window_size = 65536
    window = bytearray(window_size)
    pos = 0
    
    state = 123456789
    
    for _i in range(iterations):
        if time.perf_counter() > deadline:
            elapsed = time.perf_counter() - start_time
            print(_("Performance benchmark timed out at {elapsed:.3f} seconds.").format(elapsed=elapsed))
            _benchmark_cached_result = False
            return False

        # Linear Congruential Generator: x_{n+1} = (a * x_n + c) % m
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        
        token = state & 0xFF
        
        if token < 32:
            # Literal copy simulation
            window[pos] = token
            pos = (pos + 1) & (window_size - 1)
        else:
            # Match copy simulation
            offset = (state >> 8) & (window_size - 1)
            length = (token & 0x0F) + 3
            
            # Unrolled copy loop for realistic memory access pattern
            src_pos = (pos - offset) & (window_size - 1)
            for _j in range(length):
                window[pos] = window[src_pos]
                pos = (pos + 1) & (window_size - 1)
                src_pos = (src_pos + 1) & (window_size - 1)

    elapsed = time.perf_counter() - start_time
    print(_("Performance benchmark completed in {elapsed:.3f} seconds.").format(elapsed=elapsed))
    _benchmark_cached_result = elapsed <= config.BENCHMARK_DURATION_LIMIT
    return _benchmark_cached_result
