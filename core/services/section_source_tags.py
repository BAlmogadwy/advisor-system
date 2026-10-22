"""``TermSection.source_tag`` values the Timetable Workspace writes itself.

The two engines that make sections write these, and the code that decides
whether a section's meeting rows are the workspace's own reads them. They were
string literals in each file; named here so the writer and the reader cannot
drift apart. Anything else (``department``, ``other``, ``external``,
``scraper_timetable``, ``f_optimised_plan`` ...) came from the registrar, an
import or a scrape and is not the workspace's to rewrite.
"""

from __future__ import annotations

#: A section made by "add a section" for a required course.
PLANNED_SECTION_SOURCE_TAG = "tw_planned"

#: A section made by the automatic builders (autoplace, the solver); labelled
#: ``S{n}`` like a planned one.
AUTO_SECTION_SOURCE_TAG = "tw_auto"

#: Sections whose meeting rows are the workspace's own: they follow the
#: section's placements (``sync_meetings_from_placements`` rewrites them and
#: ``reset_scenario`` clears them), so they are not registrar data.
WORKSPACE_SECTION_SOURCE_TAGS = frozenset({PLANNED_SECTION_SOURCE_TAG, AUTO_SECTION_SOURCE_TAG})
