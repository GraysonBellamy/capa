# Reading status-bar symptoms

> **Status:** stub — content to be written.

**Audience:** operators triaging a degraded run.
**Scope:** a flowchart: which pill goes red first, what that points to, what to check.

## Will cover

- ``loop`` red first → conductor CPU starvation
- ``sat`` red, ``loop`` low → downstream stall (writer / disk / BLOCK subscriber)
- Both red → cascading CPU starvation
- ``UI overflow`` only → UI thread callback

*See also:* [Status bar](../user-guide/status-bar-guide.md).

