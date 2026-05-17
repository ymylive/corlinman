# corlinman-skills-registry

Python port of the Rust `corlinman-skills` crate.

Parses openclaw-style `SKILL.md` files (YAML frontmatter + Markdown body) and
checks runtime requirements (binaries on `$PATH`, config keys, env vars).

## Public API

```python
from corlinman_skills_registry import (
    Skill,
    SkillRequirements,
    SkillRegistry,
    SkillLoadError,
    MissingFieldError,
    DuplicateNameError,
    YamlParseError,
)

reg = SkillRegistry.load_from_dir("./skills")
skill = reg.get("web_search")
problems = reg.check_requirements("web_search", config_lookup=lambda key: cfg.get(key))
```
