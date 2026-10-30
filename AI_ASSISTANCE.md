# Authorship and AI-assistance statement

## AI coding tools used
- **Claude (Anthropic)**: generated the initial implementation of this project (application code,
  automated tests, synthetic sample documents, architecture diagram and documentation) from my
  description of the assessment requirements.

## What I did personally
- Defined the requirements and chose the approach (pluggable LLM with an offline mode, a single
  web service, database + email + webhook automation).
- Set up and ran the project on my own Windows machine (Python 3.14, VS Code), resolving
  environment issues along the way.
- Ran the full automated test suite (47 tests passing) and manually tested the workflows in the
  browser: finance analysis and calculation-error detection, HR policy conflicts, missing
  information, and access control as both Administrator and Standard User.
- Published the code to this GitHub repository and recorded the demonstration video.

## External assistance
None, apart from the AI tool listed above.

## Understanding
I have reviewed the implementation and can explain, modify and troubleshoot it, in particular
the access-control path (`security.py` → SQL filters in `retrieval.py` / `advisors.py`), the
finance calculations and verification (`finance.py`), and the automation pipeline
(`automation.py`).