# IR composition curriculum — revision 2

Revision 2 is a corrected version of the same 32 intent IDs in the parent
cohort. It is not 32 additional intentions. The original 142-file design
freeze and its candidate failures remain intact under the parent directory.

Every design still has one expression hole and three fixed candidate
expressions; this is finite-choice body fill, not free-form code generation.
Training and evaluation inputs are disjoint. The independent Python reference
uses arbitrary-precision arithmetic with explicit signed-int64 wrapping, and
the Go reference oracle is compiled separately. Candidate source bodies are
also compiled and run individually against both finite suites.

The revision corrects gold-candidate rotation labels, adds input 10 to the
reassigned-valid-flag training suite, and restructures the Boolean-local
skeletons so shared locals are used by the common guard in every candidate.
No unused-local blank assignments are inserted. Change reasons and before/after
vectors are in `revision-justifications.json`.

Original and revision-2 candidate validity, executions, and discrimination
have separate denominators. Source-unit completeness is a separate pinned
Gooo receipt dimension. Passing the declared values is not a full-domain
correctness claim. The external feedback arm is generated only from actual
compiled revision-2 candidate outputs, and observations remain advisory rather
than authenticated CI proof or semantic authority. Preparation and Go replay
make zero provider calls.
