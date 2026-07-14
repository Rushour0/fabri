# Agency frame

Complete this before generating files. One agency should own one repeatable
deliverable, not an entire department.

| Field | Required decision |
| --- | --- |
| Agency name | Kebab-case name and one-sentence purpose. |
| Target persona | Who requests and consumes the deliverable? |
| One deliverable | The single file, record, or bounded result the agency produces. |
| Inputs and boundary | Required source files/data; paths, systems, and actions that are out of scope. |
| Specialist roles | The smallest fixed set of roles; state each role's artifact or decision. |
| Proof-bar metric | An observable pass condition, such as required headings, all source items covered, or a test suite. |
| Approval gate | Deterministic verifier, named human approver, or both; state what happens on failure. |
| Provider and budget | Available provider/key environment variable, maximum parent steps, child steps, and cost ceiling if needed. |
| Memory scope | A stable collection name and whether past related runs should be retained. |

Stop and ask for clarification when the deliverable, proof bar, or approval gate
is blank. A role list without those fields is a collection of prompts, not an
agency contract.
