//! `prompts` capability adapter — exposes `corlinman-skills` skills as
//! parameterised MCP prompts.
//!
//! ## Mapping
//!
//! | Skill field        | MCP `Prompt` field |
//! |--------------------|--------------------|
//! | `name`             | `name`             |
//! | `description`      | `description`      |
//! | `body_markdown`    | `messages[0].content.text` (single `user` turn) |
//! | (no params today)  | `arguments = []`   |
//!
//! Skills don't yet declare an argument schema (the design notes this:
//! `SkillRequirements` extension is C2-or-later). Iter 6 emits an empty
//! `arguments` array; the wire shape is upward-compatible — adding
//! arguments later is purely additive.
//!
//! ## Method routing
//!
//! - `prompts/list` → [`PromptsAdapter::list_prompts`]
//! - `prompts/get`  → [`PromptsAdapter::get_prompt`]
//!
//! Both honour [`SessionContext::prompts_allowed`] for ACL.

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{json, Value as JsonValue};

use corlinman_skills::SkillRegistry;

use crate::adapters::{CapabilityAdapter, SessionContext};
use crate::error::McpError;
use crate::schema::prompts::{
    GetParams, GetResult, ListResult, Prompt, PromptContent, PromptMessage, PromptRole,
};

/// MCP method-name constants for the prompts capability.
pub const METHOD_LIST: &str = "prompts/list";
pub const METHOD_GET: &str = "prompts/get";

/// Adapter that maps an [`Arc<SkillRegistry>`] onto MCP's `prompts/*`
/// surface.
pub struct PromptsAdapter {
    skills: Arc<SkillRegistry>,
}

impl PromptsAdapter {
    pub fn new(skills: Arc<SkillRegistry>) -> Self {
        Self { skills }
    }

    /// Build the `prompts/list` response, filtered by
    /// `ctx.prompts_allowed`.
    pub fn list_prompts(&self, ctx: &SessionContext) -> ListResult {
        let mut out: Vec<Prompt> = Vec::new();
        for skill in self.skills.iter() {
            if !ctx.allows_prompt(&skill.name) {
                continue;
            }
            out.push(Prompt {
                name: skill.name.clone(),
                description: if skill.description.is_empty() {
                    None
                } else {
                    Some(skill.description.clone())
                },
                arguments: Vec::new(),
            });
        }
        out.sort_by(|a, b| a.name.cmp(&b.name));
        ListResult {
            prompts: out,
            next_cursor: None,
        }
    }

    /// Build the `prompts/get` response. Unknown name → -32602
    /// `InvalidParams` with the offending name echoed back via
    /// `data`. Allowlist denial → same code, distinct message.
    pub fn get_prompt(
        &self,
        params: GetParams,
        ctx: &SessionContext,
    ) -> Result<GetResult, McpError> {
        if !ctx.allows_prompt(&params.name) {
            return Err(McpError::invalid_params_with(
                format!("prompt '{}' is not allowed by this token", params.name),
                json!({"name": params.name}),
            ));
        }

        let skill = self.skills.get(&params.name).ok_or_else(|| {
            McpError::invalid_params_with(
                format!("unknown prompt '{}'", params.name),
                json!({"name": params.name.clone()}),
            )
        })?;

        // Skill arguments aren't modelled today; we silently accept any
        // `arguments` payload but don't substitute it into the body.
        // Future: render `skill.body_markdown` through a templating
        // pass keyed on `params.arguments`.
        let body = skill.body_markdown.clone();

        Ok(GetResult {
            description: if skill.description.is_empty() {
                None
            } else {
                Some(skill.description.clone())
            },
            messages: vec![PromptMessage {
                role: PromptRole::User,
                content: PromptContent::text(body),
            }],
        })
    }
}

#[async_trait]
impl CapabilityAdapter for PromptsAdapter {
    fn capability_name(&self) -> &'static str {
        "prompts"
    }

    async fn handle(
        &self,
        method: &str,
        params: JsonValue,
        ctx: &SessionContext,
    ) -> Result<JsonValue, McpError> {
        match method {
            METHOD_LIST => {
                let list = self.list_prompts(ctx);
                serde_json::to_value(list)
                    .map_err(|e| McpError::Internal(format!("prompts/list: serialize result: {e}")))
            }
            METHOD_GET => {
                let parsed: GetParams = serde_json::from_value(params).map_err(|e| {
                    McpError::invalid_params(format!("prompts/get: bad params: {e}"))
                })?;
                let result = self.get_prompt(parsed, ctx)?;
                serde_json::to_value(result)
                    .map_err(|e| McpError::Internal(format!("prompts/get: serialize result: {e}")))
            }
            other => Err(McpError::MethodNotFound(other.to_string())),
        }
    }
}

// ---------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    /// Build a `SkillRegistry` from a tempdir with the supplied skills.
    /// Each `(name, description, body)` becomes a `<name>.md` with
    /// frontmatter + body.
    fn make_registry(skills: &[(&str, &str, &str)]) -> (Arc<SkillRegistry>, tempfile::TempDir) {
        let tmp = tempfile::tempdir().unwrap();
        for (name, desc, body) in skills {
            let path = tmp.path().join(format!("{name}.md"));
            let mut f = std::fs::File::create(&path).unwrap();
            // Skill frontmatter: name + description are the minimum.
            let frontmatter = format!("---\nname: {name}\ndescription: {desc}\n---\n{body}");
            f.write_all(frontmatter.as_bytes()).unwrap();
        }
        let reg = SkillRegistry::load_from_dir(tmp.path()).expect("skill registry load");
        (Arc::new(reg), tmp)
    }

    #[tokio::test]
    async fn list_returns_one_prompt_per_skill_sorted() {
        let (reg, _tmp) = make_registry(&[
            ("zeta-skill", "z desc", "Z body"),
            ("alpha-skill", "a desc", "A body"),
        ]);
        let adapter = PromptsAdapter::new(reg);
        let result = adapter.list_prompts(&SessionContext::permissive());
        let names: Vec<_> = result.prompts.iter().map(|p| p.name.clone()).collect();
        assert_eq!(
            names,
            vec!["alpha-skill".to_string(), "zeta-skill".to_string()]
        );
        // Argument schema is empty by design (skills lack params today).
        assert!(result.prompts[0].arguments.is_empty());
        assert_eq!(result.prompts[0].description.as_deref(), Some("a desc"));
    }

    #[tokio::test]
    async fn list_filters_by_allowlist() {
        let (reg, _tmp) = make_registry(&[
            ("kb-search", "x", "x"),
            ("kb-summary", "x", "x"),
            ("other-thing", "x", "x"),
        ]);
        let adapter = PromptsAdapter::new(reg);
        let ctx = SessionContext {
            prompts_allowed: vec!["kb-*".to_string()],
            ..Default::default()
        };
        let result = adapter.list_prompts(&ctx);
        let names: Vec<_> = result.prompts.iter().map(|p| p.name.clone()).collect();
        assert_eq!(
            names,
            vec!["kb-search".to_string(), "kb-summary".to_string()]
        );
    }

    #[tokio::test]
    async fn get_returns_skill_body_as_user_message() {
        let (reg, _tmp) = make_registry(&[("foo", "foo desc", "Step 1.\nStep 2.")]);
        let adapter = PromptsAdapter::new(reg);
        let result = adapter
            .get_prompt(
                GetParams {
                    name: "foo".to_string(),
                    arguments: JsonValue::Null,
                },
                &SessionContext::permissive(),
            )
            .unwrap();
        assert_eq!(result.description.as_deref(), Some("foo desc"));
        assert_eq!(result.messages.len(), 1);
        assert_eq!(result.messages[0].role, PromptRole::User);
        match &result.messages[0].content {
            PromptContent::Text { text } => {
                assert!(text.contains("Step 1."));
                assert!(text.contains("Step 2."));
            }
        }
    }

    #[tokio::test]
    async fn get_unknown_name_returns_invalid_params_with_name_echoed() {
        let (reg, _tmp) = make_registry(&[("foo", "x", "x")]);
        let adapter = PromptsAdapter::new(reg);
        let err = adapter
            .get_prompt(
                GetParams {
                    name: "ghost".to_string(),
                    arguments: JsonValue::Null,
                },
                &SessionContext::permissive(),
            )
            .expect_err("must error");
        assert_eq!(err.jsonrpc_code(), -32602);
        match err {
            McpError::InvalidParams { data, message } => {
                assert!(message.contains("ghost"));
                assert_eq!(data, Some(json!({"name": "ghost"})));
            }
            other => panic!("expected InvalidParams, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn get_disallowed_name_returns_invalid_params_with_distinct_message() {
        let (reg, _tmp) = make_registry(&[("foo", "x", "x")]);
        let adapter = PromptsAdapter::new(reg);
        let ctx = SessionContext {
            prompts_allowed: vec!["other-*".to_string()],
            ..Default::default()
        };
        let err = adapter
            .get_prompt(
                GetParams {
                    name: "foo".to_string(),
                    arguments: JsonValue::Null,
                },
                &ctx,
            )
            .expect_err("must error");
        assert_eq!(err.jsonrpc_code(), -32602);
        match err {
            McpError::InvalidParams { message, data } => {
                assert!(
                    message.contains("not allowed"),
                    "expected ACL message, got {message:?}"
                );
                assert_eq!(data, Some(json!({"name": "foo"})));
            }
            other => panic!("expected InvalidParams (ACL), got {other:?}"),
        }
    }

    #[tokio::test]
    async fn handle_routes_through_capability_adapter_trait() {
        let (reg, _tmp) = make_registry(&[("foo", "desc", "body")]);
        let adapter = PromptsAdapter::new(reg);
        assert_eq!(adapter.capability_name(), "prompts");

        let value = adapter
            .handle(
                "prompts/list",
                JsonValue::Null,
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        let parsed: ListResult = serde_json::from_value(value).unwrap();
        assert_eq!(parsed.prompts.len(), 1);
        assert_eq!(parsed.prompts[0].name, "foo");

        let err = adapter
            .handle(
                "prompts/bogus",
                JsonValue::Null,
                &SessionContext::permissive(),
            )
            .await
            .expect_err("unknown method must error");
        assert!(matches!(err, McpError::MethodNotFound(_)));
    }
}
