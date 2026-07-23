@AGENTS.md

# CLAUDE.md

Claude Code-specific behavioral preferences. Architecture, commands, and conventions are in AGENTS.md.

## Behavioral Preferences

- Do NOT include `Co-Authored-By` lines in commits
- Before running tests or linters, verify the database is running with `pg_isready -h localhost -p 15432`
- Always format code with black before creating commits
- Use dotted module paths for test commands, never file paths
