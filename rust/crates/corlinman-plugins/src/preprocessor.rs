//! messagePreprocessor chain — runs before context_assembler sends messages to the model.
//
// TODO: load `messagePreprocessor/*.js` via the persistent Node host (plan §14 R4);
//       order is defined by `config.MessagePreprocessorOrder` string list.
// TODO: each preprocessor sees `{messages, context}` and may mutate; snapshot
//       before-and-after for the golden-snapshot test suite.
