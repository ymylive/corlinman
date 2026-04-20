# corlinman-agent

Self-hosted reasoning loop for corlinman. No LangChain; direct control over
``chat_stream`` + OpenAI-standard ``tool_calls`` handling + RAG/DailyNote/Var
placeholder assembly + tool-call approval gating.

Exposed to Rust gateway via the gRPC ``Agent.Chat`` bidi stream
(see ``proto/corlinman/v1/agent.proto``).
