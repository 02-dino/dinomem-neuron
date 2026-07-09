---
name: Bug report
about: Something is broken
labels: bug
---

**Describe the bug**
A clear description of what went wrong.

**Which layer is affected?**
- [ ] L2 — memory_graph.py
- [ ] L3 — memory_synthesis.py / contradiction_check.py
- [ ] L4 — memory_promote.py / generate_topic_index.py
- [ ] RAG — docs_ingest.py / docs_search.py
- [ ] Structured data — data_register.py / data_query.py
- [ ] install.sh

**To reproduce**
Steps to reproduce the behavior.

**Logs**
```
paste relevant log output here
(check <workspace>/logs/memory_graph.log, memory_synthesis.log, memory_promote.log, docs_ingest.log)
```

**Environment**
- OS: [e.g. Ubuntu 22.04]
- OpenClaw version: [run `openclaw --version`]
- Python version: [run `python3 --version`]
- dinomem version: [check github/dinomem tag]
