"""
Benchmark script for PII Faker.

Measures throughput on generated data to verify performance claims.
Run with: python -m pii_faker.tests.benchmark
"""

import time
import random
import string
from typing import List

import polars as pl
from faker import Faker

from pii_faker.core.config import PIIConfig, PIIMode
from pii_faker.core.detector import PIIColumnDetector
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.mapper import PIIMapper


def generate_test_data(n_rows: int, n_unique_names: int = 1000) -> pl.DataFrame:
    """Generate test DataFrame with PII columns."""
    fake = Faker()
    Faker.seed(42)

    # Generate unique names
    unique_names = [fake.name() for _ in range(n_unique_names)]
    names = [random.choice(unique_names) for _ in range(n_rows)]

    # Generate emails
    unique_emails = [fake.email() for _ in range(n_unique_names)]
    emails = [random.choice(unique_emails) for _ in range(n_rows)]

    # Generate phone numbers
    unique_phones = [fake.phone_number() for _ in range(n_unique_names)]
    phones = [random.choice(unique_phones) for _ in range(n_rows)]

    # Generate non-PII data
    data_col = [''.join(random.choices(string.ascii_lowercase, k=10)) for _ in range(n_rows)]

    return pl.DataFrame({
        'patient_name': names,
        'email': emails,
        'phone': phones,
        'data': data_col,
    })


def benchmark_detection(df: pl.DataFrame, config: PIIConfig, iterations: int = 10) -> dict:
    """Benchmark PII detection."""
    detector = PIIColumnDetector(config)

    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        pii_cols = detector.detect(df)
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    return {
        'operation': 'detection',
        'iterations': iterations,
        'mean_ms': sum(times) / len(times) * 1000,
        'min_ms': min(times) * 1000,
        'max_ms': max(times) * 1000,
        'pii_columns_found': len(pii_cols),
    }


def benchmark_generation(df: pl.DataFrame, config: PIIConfig, iterations: int = 3) -> dict:
    """Benchmark fake data generation."""
    detector = PIIColumnDetector(config)
    pii_cols = detector.detect(df)

    generator = PIIGenerator(config)

    times = []
    for _ in range(iterations):
        test_df = df.clone()
        start = time.perf_counter()

        for col_info in pii_cols:
            test_df, _ = generator.process_column(test_df, col_info)

        elapsed = time.perf_counter() - start
        times.append(elapsed)

    rows_per_sec = len(df) / (sum(times) / len(times))

    return {
        'operation': 'generation',
        'iterations': iterations,
        'n_rows': len(df),
        'mean_ms': sum(times) / len(times) * 1000,
        'rows_per_sec': rows_per_sec,
        'pii_columns_processed': len(pii_cols),
    }


def benchmark_unique_values_approach(df: pl.DataFrame, config: PIIConfig) -> dict:
    """Benchmark the unique-values-first approach vs naive approach."""
    generator = PIIGenerator(config)

    # Unique-values-first approach
    start = time.perf_counter()
    mapping = generator.build_mapping(df['patient_name'], 'name', 'patient_name')
    unique_time = time.perf_counter() - start

    n_unique = len(mapping)
    n_total = len(df)

    return {
        'operation': 'unique_values_approach',
        'n_rows': n_total,
        'n_unique_values': n_unique,
        'unique_mapping_time_ms': unique_time * 1000,
        'reduction_factor': n_total / n_unique if n_unique > 0 else 0,
    }


def run_benchmarks():
    """Run all benchmarks."""
    print("=" * 60)
    print("PII Faker Performance Benchmarks")
    print("=" * 60)

    config = PIIConfig(mode=PIIMode.FAKE, seed=42)

    # Test different sizes
    sizes = [10_000, 100_000, 500_000]

    for n_rows in sizes:
        print(f"\n{'─' * 60}")
        print(f"Dataset: {n_rows:,} rows")
        print(f"{'─' * 60}")

        df = generate_test_data(n_rows, n_unique_names=1000)

        # Benchmark detection
        detect_result = benchmark_detection(df, config)
        print(f"\nDetection:")
        print(f"  Mean: {detect_result['mean_ms']:.2f} ms")
        print(f"  PII columns found: {detect_result['pii_columns_found']}")

        # Benchmark generation
        gen_result = benchmark_generation(df, config)
        print(f"\nGeneration (fake):")
        print(f"  Mean: {gen_result['mean_ms']:.2f} ms")
        print(f"  Throughput: {gen_result['rows_per_sec']:,.0f} rows/sec")

        # Benchmark unique values approach
        unique_result = benchmark_unique_values_approach(df, config)
        print(f"\nUnique Values Approach:")
        print(f"  Total rows: {unique_result['n_rows']:,}")
        print(f"  Unique values: {unique_result['n_unique_values']:,}")
        print(f"  Reduction factor: {unique_result['reduction_factor']:.1f}x")
        print(f"  Mapping time: {unique_result['unique_mapping_time_ms']:.2f} ms")

    print("\n" + "=" * 60)
    print("Benchmarks complete!")
    print("=" * 60)


if __name__ == "__main__":
    run_benchmarks()
