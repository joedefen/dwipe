"""Shared verification logic and statistical analysis.

Both the legacy ``WipeJob.verify_partition`` path and the task-based
``VerifyTask.run_task`` path use these helpers so there is exactly one copy of
the (hardware-validated) verification algorithm.

``verify_device`` opens the device through the ``device_io`` seam, samples 100
equal sections at ``verify_pct`` percent, applies a fast-fail zero check, and
performs chi-square-like randomness analysis on non-zero data.
"""
# pylint: disable=broad-exception-caught
import os
import random

from .WipeTask import WipeTask
from .Utils import Utils


def analyze_section_randomness(byte_counts, total_samples):
    """Analyse whether a section's byte distribution looks random.

    Returns:
        tuple[str, dict]: (result, stats) where result is one of
        "random", "zeroed", "not-wiped" or "insufficient-data".
    """
    if total_samples < 100:
        return "insufficient-data", {"samples": total_samples}

    # Calculate statistics
    max_count = max(byte_counts)
    max_freq = max_count / total_samples

    # Count unique bytes seen
    unique_bytes = sum(1 for count in byte_counts if count > 0)

    # Count completely unused bytes
    unused_bytes = sum(1 for count in byte_counts if count == 0)

    # Calculate expected frequency and variance
    expected = total_samples / 256
    if expected > 0:
        # Coefficient of variation (measure of dispersion)
        variance = sum((count - expected) ** 2 for count in byte_counts) / 256
        std_dev = variance ** 0.5
        cv = std_dev / expected
    else:
        cv = float('inf')

    # Decision logic for "random"
    # Good random data should:
    # 1. Use most byte values (>200 unique)
    # 2. No single byte dominates (<2% frequency)
    # 3. Relatively even distribution (CV < 2.0)
    # 4. Not too many zeros (if it's supposed to be random, not zeroed)
    is_random = (unique_bytes > 200 and      # >78% of bytes used
                 max_freq < 0.02 and         # No byte > 2%
                 cv < 2.0 and               # Not too lumpy
                 byte_counts[0] / total_samples < 0.5)  # Not mostly zeros

    stats = {
        "samples": total_samples,
        "max_freq": max_freq,
        "unique_bytes": unique_bytes,
        "unused_bytes": unused_bytes,
        "cv": cv,
        "zero_freq": byte_counts[0] / total_samples if total_samples > 0 else 0
    }

    if is_random:
        return "random", stats

    # Check if it's zeros
    if byte_counts[0] / total_samples > 0.95:
        return "zeroed", stats
    return "not-wiped", stats


def verify_device(io, device_path, total_size, verify_pct, expected_pattern,
                  fast_fail=False, is_aborted=None, on_progress=None):
    """Verify a device against ``expected_pattern``.

    Args:
        io: device I/O implementation exposing ``open_read``/``lseek``/
            ``read``/``close`` (normally the ``device_io`` module).
        device_path: path to the device.
        total_size: total device size in bytes.
        verify_pct: percentage of each section to sample (0 = skip).
        expected_pattern: "zeroed", "random", or None (auto-detect).
        fast_fail: enable the fast memcmp zero check (only applies when
            ``expected_pattern == "zeroed"``).
        is_aborted: zero-arg callable returning True to stop early.
        on_progress: callable(bytes_read) invoked as data is consumed; used by
            callers to update their own progress counters.

    Returns:
        tuple: (verify_result, expected_pattern, section_results)
    """
    if is_aborted is None:
        def is_aborted():
            return False
    if on_progress is None:
        def on_progress(_n):
            pass

    if verify_pct == 0:
        return "skipped", expected_pattern, []

    # Fast-fail for zeros (only when the caller opted in)
    fast_fail_zeros = fast_fail and expected_pattern == "zeroed"

    # For unmarked disks: track if ALL bytes are zero
    all_zeros = (expected_pattern is None)

    section_results = []

    try:
        fd = io.open_read(device_path)
        try:
            read_chunk_size = 64 * 1024  # 64KB chunks
            SAMPLE_STEP = 23  # Sample every 23rd byte (~4% of data) - prime for even distribution

            # Skip marker area
            marker_skip = WipeTask.BUFFER_SIZE
            usable_size = total_size - marker_skip

            # Divide disk into 100 sections for sampling
            num_sections = 100
            section_size = usable_size // num_sections

            # Pre-allocated zero pattern for fast comparison
            ZERO_PATTERN_64K = b'\x00' * (64 * 1024)

            # Track if any section failed
            overall_failed = False
            failure_reason = ""

            for section_idx in range(num_sections):
                if is_aborted() or overall_failed:
                    break

                # Reset analysis for THIS SECTION
                section_byte_counts = [0] * 256
                section_samples = 0
                section_found_nonzero = False

                # Calculate bytes to verify in this section
                bytes_in_section = min(section_size, usable_size - section_idx * section_size)
                bytes_to_verify = int(bytes_in_section * verify_pct / 100)

                if bytes_to_verify == 0:
                    section_results.append((section_idx, "skipped", {}))
                    continue

                # Random offset within section
                if bytes_to_verify < bytes_in_section:
                    offset_in_section = random.randint(0, bytes_in_section - bytes_to_verify)
                else:
                    offset_in_section = 0

                read_pos = marker_skip + (section_idx * section_size) + offset_in_section
                verified_in_section = 0

                # Seek to position in this section
                io.lseek(fd, read_pos, os.SEEK_SET)

                # Read and analyze THIS SECTION
                while verified_in_section < bytes_to_verify:
                    if is_aborted():
                        break

                    chunk_size = min(read_chunk_size, bytes_to_verify - verified_in_section)

                    data = io.read(fd, chunk_size)
                    if not data:
                        break

                    # FAST zero check for zeroed pattern
                    if fast_fail_zeros:
                        # Ultra-fast: compare against pre-allocated zero pattern
                        if memoryview(data) != ZERO_PATTERN_64K[:len(data)]:
                            failed_offset = read_pos + verified_in_section
                            overall_failed = True
                            failure_reason = f"non-zero at {Utils.human(failed_offset)}"
                            break

                    # FAST check for unmarked disks (looking for all zeros)
                    if all_zeros and not section_found_nonzero:
                        # Fast check: use bytes.count() which is C-optimized
                        if data.count(0) != len(data):
                            section_found_nonzero = True

                    # RANDOM pattern analysis (always collect data for analysis)
                    # Use memoryview for fast slicing
                    mv = memoryview(data)
                    data_len = len(data)

                    # Sample every SAMPLE_STEP-th byte
                    for i in range(0, data_len, SAMPLE_STEP):
                        section_byte_counts[mv[i]] += 1
                        section_samples += 1

                    verified_in_section += len(data)
                    on_progress(len(data))

                # After reading section, analyze it
                if overall_failed:
                    break

                # Determine section result
                if fast_fail_zeros:
                    # Already passed zero check if we got here
                    section_result = "zeroed"
                    section_stats = {}
                elif all_zeros:
                    if not section_found_nonzero:
                        section_result = "zeroed"
                        section_stats = {}
                    else:
                        # Need to check if it's random
                        section_result, section_stats = analyze_section_randomness(
                            section_byte_counts, section_samples
                        )
                else:  # Expected random
                    section_result, section_stats = analyze_section_randomness(
                        section_byte_counts, section_samples
                    )

                # Store section result
                section_results.append((section_idx, section_result, section_stats))

                # Check if section failed
                if (expected_pattern == "random" and section_result != "random") or \
                   (expected_pattern == "zeroed" and section_result != "zeroed") or \
                   (expected_pattern is None and section_result == "not-wiped"):
                    overall_failed = True
                    failure_reason = f"section {section_idx}: {section_result}"
                    break
        finally:
            io.close(fd)
    except Exception:
        return "error", expected_pattern, section_results

    # Determine overall result
    if overall_failed:
        if expected_pattern == "zeroed":
            verify_result = f"not-wiped ({failure_reason})"
        elif expected_pattern == "random":
            verify_result = f"not-wiped ({failure_reason})"
        else:  # unmarked
            # Count section results
            zeroed_sections = sum(1 for _, result, _ in section_results if result == "zeroed")
            random_sections = sum(1 for _, result, _ in section_results if result == "random")
            total_checked = len([r for _, r, _ in section_results if r != "skipped"])

            if zeroed_sections == total_checked:
                verify_result = "zeroed"
                expected_pattern = "zeroed"
            elif random_sections == total_checked:
                verify_result = "random"
                expected_pattern = "random"
            else:
                verify_result = f"mixed ({failure_reason})"
    else:
        # All sections passed
        if expected_pattern == "zeroed":
            verify_result = "zeroed"
        elif expected_pattern == "random":
            verify_result = "random"
        else:  # unmarked
            # Determine from section consensus
            zeroed_sections = sum(1 for _, result, _ in section_results if result == "zeroed")
            random_sections = sum(1 for _, result, _ in section_results if result == "random")

            if zeroed_sections > random_sections:
                verify_result = "zeroed"
                expected_pattern = "zeroed"
            else:
                verify_result = "random"
                expected_pattern = "random"

    return verify_result, expected_pattern, section_results
