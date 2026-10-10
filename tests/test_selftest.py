"""The selftest a user runs to check their SolidWorks before reporting an issue.

The runner's contract (every check reported, every document closed, a loose
sketch is a failure) is tested with a stand-in session; the checks themselves
need a running SolidWorks.
"""

import os
import pathlib

import pytest

from solidworks_mcp import selftest
from solidworks_mcp.constants import LENGTH_UNITS
from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.selftest import CHECKS, release_year, run_checks, summary


class StandInSession:
    """Records the documents a check opens and closes, like SolidWorksSession."""

    def __init__(self, loose_sketches=()):
        self._model = None
        self.loose_sketches = list(loose_sketches)
        self.closed = 0
        self.guarded = 0

    def run_guarded(self, fn, *args):
        self.guarded += 1
        return fn(*args)

    def new_part(self):
        self._model = object()

    def close_part(self):
        self._model = None
        self.closed += 1

    def _under_defined_sketches(self):
        return self.loose_sketches


def builds(sw):
    sw.new_part()
    return "8000.000 mm3"


def breaks(sw):
    sw.new_part()
    raise SolidWorksError("FeatureCut4 failed")


def test_a_failing_check_is_reported_and_the_next_one_still_runs():
    sw = StandInSession()

    results = list(run_checks(sw, [("breaks", breaks), ("builds", builds)]))

    assert results == [("breaks", False, "SolidWorksError: FeatureCut4 failed"),
                       ("builds", True, "8000.000 mm3")], results
    assert sw.closed == 2, "a check's part must be closed even when it fails, or the user's SolidWorks fills up"
    assert sw.guarded == 2, (
        "every check must run as a tool call does: SOLIDWORKS 2026 SP4.0 crashed while sketching otherwise"
    )


def test_a_check_that_leaves_a_sketch_loose_fails():
    sw = StandInSession(loose_sketches=["Sketch1 (under defined)"])

    [(label, passed, detail)] = run_checks(sw, [("builds", builds)])

    assert not passed and "Sketch1 (under defined)" in detail, (
        "on another SolidWorks release the relations may not stick; the selftest must say so"
    )


def test_the_summary_counts_what_passed():
    assert summary([("a", True, ""), ("b", False, "x"), ("c", True, "")]).startswith("2 of 3 checks passed")


def test_the_report_links_to_a_form_that_exists(monkeypatch, capsys):
    """The link comes from the package's Issues URL plus the name of a form in
    .github/ISSUE_TEMPLATE; renaming the form must not leave users a dead link."""

    class PackageMetadata:
        def get_all(self, field):
            assert field == "Project-URL"
            return ["Homepage, https://example.org", "Issues, https://example.org/issues"]

    monkeypatch.setattr(selftest.importlib.metadata, "metadata", lambda name: PackageMetadata())

    selftest._report_hint()

    link = capsys.readouterr().out.splitlines()[-1]
    assert link.startswith("https://example.org/issues/new?template="), link
    form = link.split("template=")[1]
    assert (pathlib.Path(".github/ISSUE_TEMPLATE") / form).is_file(), f"the selftest links to a missing form: {form}"


@pytest.mark.parametrize("revision,year", [("34.3.0", 2026), ("33.0.1", 2025), ("28.1.0", 2020)])
def test_the_release_year_follows_the_major_revision(revision, year):
    assert release_year(revision) == year


@pytest.mark.solidworks
def test_every_check_passes_on_a_working_installation(sw):
    """The checks' own hand calculations must be right: a wrong one sends every
    user who runs the selftest a false alarm. The selftest must also close every
    document it opens."""
    documents_before = len(sw._sw.GetDocuments() or ())

    results = list(run_checks(sw, CHECKS))

    assert [r for r in results if not r[1]] == [], results
    assert len(sw._sw.GetDocuments() or ()) == documents_before, "the selftest left documents open"


@pytest.mark.solidworks
def test_the_installation_is_described(sw):
    # 3DEXPERIENCE's default template is a name ('~BLANK_PART_TEMPLATE.prtdot'),
    # not a file, so whether it is found must come from the disk, not be assumed
    info = sw.describe_installation()

    assert release_year(info["revision"]) >= 2020 and info["language"], info
    assert info["units"] in LENGTH_UNITS.values(), info
    for kind in ("part_template", "assembly_template"):
        assert info[kind]["found"] == os.path.isfile(info[kind]["path"]), info
