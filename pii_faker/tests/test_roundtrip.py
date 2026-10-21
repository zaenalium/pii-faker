"""Property-based round-trip tests (roadmap 0.2).

restore(process(x)) == x for fake + token modes across flat, multi-column,
multi-sheet, nested and JSON-string data. These guard D5, D8 and D9.
"""

import datetime
import string

import polars as pl
import pytest
from hypothesis import given, settings, strategies as st

from pii_faker.core import process_dataframe, restore_dataframe
from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle
from pii_faker.core.pipeline import process_sheets, restore_sheets
from pii_faker.core.restorer import PIIRestorer

safe_text = st.text(
    alphabet=string.ascii_letters + " -'",
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip())

emails = st.builds(
    lambda u, d: f"{u}@{d}.com",
    st.text(alphabet=string.ascii_lowercase, min_size=2, max_size=8),
    st.text(alphabet=string.ascii_lowercase, min_size=2, max_size=6),
)


def _frames_equal(a: pl.DataFrame, b: pl.DataFrame) -> bool:
    assert a.columns == b.columns, f"{a.columns} != {b.columns}"
    assert a.schema == b.schema, f"{a.schema} != {b.schema}"
    assert a.to_dicts() == b.to_dicts()
    return True


@given(
    names=st.lists(safe_text, min_size=1, max_size=15),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_fake_flat_round_trip(names, seed):
    df = pl.DataFrame({"patient_name": names})
    config = PIIConfig(seed=seed)
    processed, mapper = process_dataframe(df, config)
    assert _frames_equal(restore_dataframe(processed, mapper), df)
    fakes = processed["patient_name"].to_list()
    assert len(set(fakes)) == len(set(names))


@given(
    rows=st.lists(
        st.tuples(safe_text, emails, st.integers(min_value=0, max_value=99999)),
        min_size=1,
        max_size=15,
    ),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_fake_multi_column_round_trip(rows, seed):
    names = [r[0] for r in rows]
    mails = [r[1] for r in rows]
    codes = [r[2] for r in rows]
    df = pl.DataFrame({"name": names, "email": mails, "code": codes})
    config = PIIConfig(
        seed=seed,
        extra_pii_columns=["code"],
        override_defaults=False,
    )
    processed, mapper = process_dataframe(df, config)
    assert _frames_equal(restore_dataframe(processed, mapper), df)


@given(
    s1=st.lists(safe_text, min_size=1, max_size=10),
    s2=st.lists(safe_text, min_size=1, max_size=10),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_fake_multi_sheet_round_trip(s1, s2, seed):
    shared = s1[:2] if len(s1) >= 2 else s1[:1]
    sheets = {
        "S1": pl.DataFrame({"name": s1}),
        "S2": pl.DataFrame({"name": shared + s2}),
    }
    config = PIIConfig(seed=seed)
    processed, mapper, _ = process_sheets(sheets, config)
    restored = restore_sheets(processed, mapper)
    for key in sheets:
        assert _frames_equal(restored[key], sheets[key])
    # Same original -> same fake on both sheets
    fakes_s1 = dict(zip(s1, processed["S1"]["name"].to_list()))
    fakes_s2 = dict(zip(shared + s2, processed["S2"]["name"].to_list()))
    for name in shared:
        assert fakes_s1[name] == fakes_s2[name]


@given(
    values=st.lists(safe_text, min_size=1, max_size=10),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_token_round_trip(values, seed):
    df = pl.DataFrame({"a": values, "b": list(reversed(values))})
    config = PIIConfig(
        mode=PIIMode.REDACT,
        redact_style=RedactionStyle.TOKEN,
        extra_pii_columns=["a", "b"],
        override_defaults=True,
        seed=seed,
    )
    processed, mapper = process_dataframe(df, config)
    tokens = processed["a"].to_list() + processed["b"].to_list()
    assert len(set(tokens)) == 2 * len(set(values))
    assert _frames_equal(restore_dataframe(processed, mapper), df)


@given(
    values=st.lists(safe_text, min_size=1, max_size=10),
)
@settings(max_examples=25, deadline=None)
def test_typed_token_round_trip(values):
    df = pl.DataFrame({"email": values})
    config = PIIConfig(
        mode=PIIMode.REDACT,
        redact_style=RedactionStyle.TYPED_TOKEN,
        extra_pii_columns=["email"],
        override_defaults=False,
    )
    processed, mapper = process_dataframe(df, config)
    tokens = processed["email"].to_list()
    assert all(t.startswith("<") and t.endswith(">") for t in tokens)
    assert len(set(tokens)) == len(set(values))
    assert _frames_equal(restore_dataframe(processed, mapper), df)


@given(
    names=st.lists(safe_text, min_size=1, max_size=8),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_nested_round_trip(names, seed):
    df = pl.DataFrame({
        "id": list(range(len(names))),
        "patient": [{"patient_name": n, "x": 1} for n in names],
    })
    config = PIIConfig(seed=seed)
    processed, mapper = process_dataframe(df, config)
    restored = restore_dataframe(processed, mapper)
    assert restored.to_dicts() == df.to_dicts()


@given(
    names=st.lists(safe_text, min_size=1, max_size=8),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_json_string_round_trip(names, seed):
    import json as json_module

    df = pl.DataFrame({
        "id": list(range(len(names))),
        "payload": [json_module.dumps({"patient_name": n, "k": 1}) for n in names],
    })
    config = PIIConfig(seed=seed, json_columns=["payload"])
    processed, mapper = process_dataframe(df, config)
    restored = restore_dataframe(processed, mapper)
    assert [json_module.loads(v) for v in restored["payload"].to_list()] == [
        json_module.loads(v) for v in df["payload"].to_list()
    ]


@given(
    values=st.lists(
        st.dates(min_value=datetime.date(1950, 1, 1), max_value=datetime.date(2010, 12, 31)),
        min_size=1,
        max_size=8,
    ),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_date_round_trip(values, seed):
    df = pl.DataFrame({"dob": values})
    config = PIIConfig(seed=seed, extra_pii_columns=["dob"])
    processed, mapper = process_dataframe(df, config)
    assert processed.schema["dob"] == pl.Date
    assert _frames_equal(restore_dataframe(processed, mapper), df)


@given(
    values=st.lists(st.integers(min_value=0, max_value=999999), min_size=1, max_size=10),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_int_round_trip(values, seed):
    df = pl.DataFrame({"phone": values})
    config = PIIConfig(seed=seed, extra_pii_columns=["phone"])
    processed, mapper = process_dataframe(df, config)
    assert _frames_equal(restore_dataframe(processed, mapper), df)


@given(
    originals=st.lists(safe_text, min_size=2, max_size=6),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=25, deadline=None)
def test_restore_text_no_chaining(originals, seed):
    df = pl.DataFrame({"name": originals})
    _, mapper = process_dataframe(df, PIIConfig(seed=seed))
    restorer = PIIRestorer(mapper)
    mapping = mapper.store.get_all_mappings()["name"]
    uniques = sorted(set(originals))
    fakes = [mapping[o] for o in uniques]
    text = " ; ".join(f"{a} met {b}" for a, b in zip(fakes, reversed(fakes)))
    restored = restorer.restore_text(text)
    expected = " ; ".join(f"{a} met {b}" for a, b in zip(uniques, reversed(uniques)))
    assert restored == expected
