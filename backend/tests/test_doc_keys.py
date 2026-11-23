"""Key grammar, front matter and metadata builder (ADR 0002, slice 1).

Pure unit tests: no AWS, no filesystem, no environment.
"""
from __future__ import annotations

from datetime import date

import pytest

from common import doc_keys
from common.doc_keys import (
    OPEN_ENDED,
    DocumentFormatError,
    InvalidDocumentKeyError,
    InvalidFrontMatterError,
    build_document_metadata,
    build_vector_id,
    date_to_int,
    parse_document_key,
    parse_front_matter,
)


class TestParseValidKeys:
    @pytest.mark.parametrize(
        "source_key,audience,scope_type,property_id,lease_id,doc_type,doc_id,version,scope_key",
        [
            (
                "incoming/renters/company/policies/repair-policy/v1.md",
                "renter", "company", None, None, "policies", "repair-policy", 1, "renters/company",
            ),
            (
                "incoming/staff/company/sops/water-damage/v12.md",
                "staff", "company", None, None, "sops", "water-damage", 12, "staff/company",
            ),
            (
                "incoming/renters/properties/P-001/policies/house-rules/v2.md",
                "renter", "property", "P-001", None, "policies", "house-rules", 2,
                "renters/properties/P-001",
            ),
            (
                "incoming/staff/properties/P-002/sops/heating-vendor/v1.md",
                "staff", "property", "P-002", None, "sops", "heating-vendor", 1,
                "staff/properties/P-002",
            ),
            (
                "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md",
                "renter", "lease", None, "L-0001", "contracts", "lease-contract", 1,
                "renters/leases/L-0001",
            ),
            (
                "incoming/renters/leases/L-0003/statements/utility-2026/v3.md",
                "renter", "lease", None, "L-0003", "statements", "utility-2026", 3,
                "renters/leases/L-0003",
            ),
        ],
    )
    def test_each_scope_row(
        self, source_key, audience, scope_type, property_id, lease_id, doc_type, doc_id, version, scope_key
    ):
        key = parse_document_key(source_key)
        assert (key.audience, key.scope_type) == (audience, scope_type)
        assert (key.property_id, key.lease_id) == (property_id, lease_id)
        assert (key.doc_type, key.doc_id, key.doc_version) == (doc_type, doc_id, version)
        assert key.scope_key == scope_key
        assert key.source_key == source_key
        assert key.relative_key == f"{scope_key}/{doc_type}/{doc_id}/v{version}"
        assert key.logical_doc == f"{scope_key}/{doc_type}/{doc_id}"

    def test_the_grammar_table_has_exactly_the_documented_rows(self):
        assert {(r.path_audience, r.path_scope) for r in doc_keys.SCOPE_RULES} == {
            ("staff", "company"),
            ("renters", "company"),
            ("staff", "properties"),
            ("renters", "properties"),
            ("renters", "leases"),
        }

    def test_maximum_length_ids_stay_far_below_the_vector_key_limit(self):
        key = parse_document_key(
            "incoming/renters/leases/L-" + "9" * 12 + "/contracts/" + "a" * 64 + "/v999999.md"
        )
        vector_id = build_vector_id(key, "v" * 64, 10**6)
        assert len(vector_id) < 1024 // 4


class TestRejectInvalidKeys:
    OK = "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md"

    @pytest.mark.parametrize(
        "source_key,fragment",
        [
            ("", "start with incoming/"),
            ("other/renters/company/policies/x/v1.md", "start with incoming/"),
            ("/incoming/renters/company/policies/x/v1.md", "start with incoming/"),
            ("incoming", "too short"),
            ("incoming/renters", "too short"),
            ("incoming/T001/faq.md", "unknown audience/scope"),  # the legacy layout
            ("incoming/Tenants/T001/faq.md", "unknown audience/scope"),
            ("incoming/staff/leases/L-0001/contracts/x/v1.md", "unknown audience/scope"),  # no such row
            ("incoming/tenants/company/policies/x/v1.md", "unknown audience/scope"),
            ("incoming/renters/units/U-1/policies/x/v1.md", "unknown audience/scope"),
            ("incoming//company/policies/x/v1.md", "unknown audience/scope"),
            ("incoming/renters/company/policies/x", "expected incoming/renters/company/"),
            ("incoming/renters/company/policies/x/v1.md/", "expected incoming/renters/company/"),
            ("incoming/renters/company/extra/policies/x/v1.md", "expected incoming/renters/company/"),
            ("incoming/renters/leases/contracts/x/v1.md", "expected incoming/renters/leases/{lease_id}/"),
            ("incoming/renters/properties/P-001/units/U-1/policies/x/v1.md", "expected incoming/renters/properties/"),
        ],
    )
    def test_structure(self, source_key, fragment):
        with pytest.raises(InvalidDocumentKeyError) as err:
            parse_document_key(source_key)
        assert fragment in str(err.value)

    @pytest.mark.parametrize(
        "bad",
        [
            "incoming/renters/leases/L-1/contracts/x/v1.md",  # too few digits
            "incoming/renters/leases/l-0001/contracts/x/v1.md",  # lowercase
            "incoming/renters/leases/T001/contracts/x/v1.md",  # a tenant, not a lease
            "incoming/renters/leases/L-0001 /contracts/x/v1.md",
            "incoming/renters/leases/L-0001\n/contracts/x/v1.md",
            "incoming/renters/leases/L-" + "1" * 13 + "/contracts/x/v1.md",  # over the bound
            "incoming/renters/leases/../contracts/x/v1.md",
            "incoming/renters/properties/P-01/policies/x/v1.md",
            "incoming/renters/properties/L-0001/policies/x/v1.md",  # a lease, not a property
        ],
    )
    def test_scope_ids(self, bad):
        with pytest.raises(InvalidDocumentKeyError, match="invalid (lease_id|property_id)|expected"):
            parse_document_key(bad)

    @pytest.mark.parametrize("doc_type", ["", "contract", "Contracts", "faq", "kb", ".."])
    def test_doc_type_is_a_closed_vocabulary(self, doc_type):
        with pytest.raises(InvalidDocumentKeyError, match="unknown doc_type"):
            parse_document_key(f"incoming/renters/company/{doc_type}/x/v1.md")

    @pytest.mark.parametrize(
        "doc_id",
        ["", "Repair", "repair_policy", "-repair", "repair-", "re--pair", "re pair", "a" * 65, "..", "é"],
    )
    def test_doc_id_is_a_lowercase_slug(self, doc_id):
        with pytest.raises(InvalidDocumentKeyError, match="invalid doc_id"):
            parse_document_key(f"incoming/renters/company/policies/{doc_id}/v1.md")

    @pytest.mark.parametrize(
        "filename",
        ["v0.md", "v01.md", "v1.txt", "V1.md", "v1.md.bak", "latest.md", "v.md", "v-1.md", "v1", "v1000000.md", "x.md"],
    )
    def test_filename_must_be_a_business_version(self, filename):
        with pytest.raises(InvalidDocumentKeyError, match=r"filename must be v<n>\.md"):
            parse_document_key(f"incoming/renters/company/policies/x/{filename}")

    def test_error_is_a_document_format_error_and_bounds_the_echoed_key(self):
        long_key = "incoming/" + "x" * 5000
        with pytest.raises(DocumentFormatError) as err:
            parse_document_key(long_key)
        assert isinstance(err.value, ValueError) and len(str(err.value)) < 700

    def test_the_known_good_key_is_valid(self):
        assert parse_document_key(self.OK).lease_id == "L-0001"


FM = """---
title: Reparaturrichtlinie
language: de
effective_from: 2027-01-01
---
# Reparaturen

Meldungen bitte schriftlich.
"""


class TestFrontMatterValid:
    def test_minimal(self):
        fm, body = parse_front_matter(FM)
        assert fm.title == "Reparaturrichtlinie"
        assert fm.language == "de"
        assert fm.effective_from == date(2027, 1, 1)
        assert fm.effective_to is None
        assert body.startswith("# Reparaturen") and "schriftlich" in body
        assert "title:" not in body and "---" not in body  # the block is stripped

    def test_effective_to_and_quotes_and_comments_and_blank_lines(self):
        text = (
            "---\n# a comment line\n\ntitle: 'Hausordnung # Objekt P-001'\n"
            'language: "en"\neffective_from: 2027-01-01\neffective_to: 2027-12-31\n---\nBody\n'
        )
        fm, body = parse_front_matter(text)
        assert fm.title == "Hausordnung # Objekt P-001"  # no inline comments: '#' belongs to the title
        assert fm.language == "en"
        assert fm.effective_to == date(2027, 12, 31)
        assert body == "Body\n"

    def test_same_day_window_is_allowed(self):
        text = FM.replace("---\n#", "effective_to: 2027-01-01\n---\n#", 1)
        fm, _ = parse_front_matter(text)
        assert fm.effective_to == fm.effective_from

    def test_crlf_cr_and_bom_are_normalized(self):
        for text in (FM.replace("\n", "\r\n"), FM.replace("\n", "\r"), "﻿" + FM):
            fm, body = parse_front_matter(text)
            assert fm.title == "Reparaturrichtlinie" and "Meldungen" in body

    def test_a_dash_rule_inside_the_body_is_kept(self):
        fm, body = parse_front_matter(FM + "\n---\n\nEnd\n")
        assert "\n---\n" in body and body.rstrip().endswith("End")

    def test_trailing_space_on_the_fence_is_tolerated(self):
        assert parse_front_matter(FM.replace("---\n", "--- \n"))[0].title

    def test_value_may_contain_a_colon(self):
        fm, _ = parse_front_matter(FM.replace("Reparaturrichtlinie", "Regel: Reparatur"))
        assert fm.title == "Regel: Reparatur"


def problems_of(text: str) -> list[str]:
    with pytest.raises(InvalidFrontMatterError) as err:
        parse_front_matter(text)
    return err.value.problems


class TestFrontMatterInvalid:
    def test_missing_or_unclosed_block(self):
        assert "must start with a front-matter block" in problems_of("# Just markdown\n")[0]
        assert "must start with a front-matter block" in problems_of("")[0]
        assert "not closed" in problems_of("---\ntitle: x\nlanguage: de\n")[0]

    @pytest.mark.parametrize("missing", ["title", "language", "effective_from"])
    def test_required_keys(self, missing):
        text = "\n".join(l for l in FM.split("\n") if not l.startswith(missing + ":"))
        assert any(f"missing required key '{missing}'" in p for p in problems_of(text))

    def test_unknown_key_with_hint(self):
        problems = problems_of(FM.replace("title:", "titel:", 1))
        assert any("unknown key 'titel' (did you mean 'title'?)" in p for p in problems)

    def test_duplicate_key(self):
        assert any("duplicate key 'language'" in p for p in problems_of(FM.replace("---\n#", "language: en\n---\n#", 1)))

    @pytest.mark.parametrize(
        "line,fragment",
        [
            ("  nested: x", "only flat"),
            ("\tindented: x", "only flat"),
            ("- list item", "only flat"),
            ("no colon here", "expected 'key: value'"),
            ("Title: x", "expected 'key: value' with a lowercase key"),
            ("a-b: x", "expected 'key: value'"),
            (": x", "expected 'key: value'"),
        ],
    )
    def test_line_shapes(self, line, fragment):
        assert any(fragment in p for p in problems_of(FM.replace("---\n#", line + "\n---\n#", 1)))

    def test_empty_values_are_rejected_and_not_reported_as_missing(self):
        problems = problems_of(FM.replace("title: Reparaturrichtlinie", "title:"))
        assert any("'title' is empty" in p for p in problems)
        assert not any("missing required key 'title'" in p for p in problems)

    def test_empty_optional_key_must_be_omitted(self):
        problems = problems_of(FM.replace("---\n#", "effective_to:\n---\n#", 1))
        assert any("'effective_to' is empty (omit an optional key" in p for p in problems)

    @pytest.mark.parametrize("language", ["fr", "DE", "deutsch", "de-DE"])
    def test_language_vocabulary(self, language):
        assert any("language must be one of" in p for p in problems_of(FM.replace("language: de", f"language: {language}")))

    @pytest.mark.parametrize(
        "value", ["2027-1-1", "20270101", "2027-W01-1", "01.01.2027", "2027-02-30", "2027-13-01", "tomorrow", "2027-01-01T00:00"]
    )
    def test_dates_must_be_strict_iso(self, value):
        problems = problems_of(FM.replace("2027-01-01", value))
        assert any("effective_from" in p for p in problems)

    @pytest.mark.parametrize("value", ["1999-12-31", "2101-01-01"])
    def test_year_range(self, value):
        assert any("year must be between" in p for p in problems_of(FM.replace("2027-01-01", value)))

    def test_window_must_not_end_before_it_starts(self):
        text = FM.replace("---\n#", "effective_to: 2026-12-31\n---\n#", 1)
        assert any("effective_to must not be before effective_from" in p for p in problems_of(text))

    def test_title_limits(self):
        assert any("title must be at most" in p for p in problems_of(FM.replace("Reparaturrichtlinie", "x" * 201)))
        assert any("control characters" in p for p in problems_of(FM.replace("Reparaturrichtlinie", "a\x07b")))
        assert parse_front_matter(FM.replace("Reparaturrichtlinie", "x" * 200))[0].title

    def test_body_is_required(self):
        head = FM.split("---\n#")[0] + "---\n"
        assert any("no content" in p for p in problems_of(head))
        assert any("no content" in p for p in problems_of(head + "\n  \n\n"))

    def test_every_problem_is_reported_in_one_pass(self):
        text = "---\ntitel: x\nlanguage: fr\neffective_from: 2027-1-1\n---\n"
        problems = problems_of(text)
        assert len(problems) >= 5
        joined = " | ".join(problems)
        for fragment in ("unknown key", "language must be one of", "effective_from", "missing required key 'title'", "no content"):
            assert fragment in joined


class TestMetadata:
    def fm(self, **kw):
        base = {"title": "T", "language": "de", "effective_from": date(2027, 1, 31), "effective_to": None}
        base.update(kw)
        return doc_keys.FrontMatter(**base)

    def test_lease_document(self):
        key = parse_document_key("incoming/renters/leases/L-0001/contracts/lease-contract/v2.md")
        assert build_document_metadata(key, self.fm()) == {
            "audience": "renter",
            "scope_type": "lease",
            "lease_id": "L-0001",
            "doc_type": "contracts",
            "doc_id": "lease-contract",
            "doc_version": 2,
            "language": "de",
            "effective_from": 20270131,
            "effective_to": OPEN_ENDED,
            "title": "T",
            "source_key": key.source_key,
        }

    def test_a_lease_document_carries_no_property_id(self):
        key = parse_document_key("incoming/renters/leases/L-0001/contracts/x/v1.md")
        assert "property_id" not in build_document_metadata(key, self.fm())

    def test_property_document_carries_property_but_not_lease(self):
        key = parse_document_key("incoming/staff/properties/P-001/sops/x/v1.md")
        md = build_document_metadata(key, self.fm(effective_to=date(2027, 12, 31)))
        assert md["property_id"] == "P-001" and "lease_id" not in md
        assert md["audience"] == "staff" and md["scope_type"] == "property"
        assert md["effective_to"] == 20271231

    def test_company_document_carries_neither(self):
        md = build_document_metadata(parse_document_key("incoming/renters/company/policies/x/v1.md"), self.fm())
        assert "property_id" not in md and "lease_id" not in md

    def test_dates_are_sortable_integers(self):
        assert date_to_int(date(2027, 1, 31)) == 20270131
        assert date_to_int(date(2026, 12, 31)) < date_to_int(date(2027, 1, 1)) < OPEN_ENDED

    def test_metadata_never_contains_a_tenant_id(self):
        md = build_document_metadata(parse_document_key("incoming/renters/leases/L-0001/contracts/x/v1.md"), self.fm())
        assert "tenant_id" not in md


class TestVectorId:
    KEY = parse_document_key("incoming/renters/leases/L-0001/contracts/lease-contract/v2.md")

    def test_shape_and_determinism(self):
        vid = build_vector_id(self.KEY, "3HL4kqtJlcpXroDTDmJ", 4)
        assert vid == "renters/leases/L-0001/contracts/lease-contract/v2/3HL4kqtJlcpXroDTDmJ/4"
        assert vid == build_vector_id(self.KEY, "3HL4kqtJlcpXroDTDmJ", 4)

    def test_equal_filenames_in_different_scopes_do_not_collide(self):
        a = parse_document_key("incoming/renters/leases/L-0001/contracts/x/v1.md")
        b = parse_document_key("incoming/renters/leases/L-0002/contracts/x/v1.md")
        c = parse_document_key("incoming/staff/company/policies/x/v1.md")
        d = parse_document_key("incoming/renters/company/policies/x/v1.md")
        ids = {build_vector_id(k, "ver", 0) for k in (a, b, c, d)}
        assert len(ids) == 4

    def test_rejects_an_empty_source_version(self):
        with pytest.raises(ValueError):
            build_vector_id(self.KEY, "", 0)

    def test_s3_version_ids_with_slash_and_plus_are_encoded_not_rejected(self):
        """Review finding: S3 VersionIds are opaque; AWS's own examples contain '/' and '+'."""
        vid = build_vector_id(self.KEY, "3/L4kqtJlcpXroDTDmJ+rmSp", 2)
        assert vid == "renters/leases/L-0001/contracts/lease-contract/v2/3%2FL4kqtJlcpXroDTDmJ%2BrmSp/2"
        assert vid.count("/") == build_vector_id(self.KEY, "plain", 2).count("/")  # still one segment
        assert vid == build_vector_id(self.KEY, "3/L4kqtJlcpXroDTDmJ+rmSp", 2)  # deterministic

    @pytest.mark.parametrize("index", [-1, True, 1.5, "0"])
    def test_rejects_bad_chunk_index(self, index):
        with pytest.raises(ValueError):
            build_vector_id(self.KEY, "ver", index)
