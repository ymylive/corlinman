use serde::{Deserialize, Serialize};

/// High-level channel-type categories surfaced to corlinman admin /
/// onboard UIs. Mapped to the integer codes new-api uses on its
/// `/api/channel/?type=` endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ChannelType {
    Llm,
    Embedding,
    Tts,
}

impl ChannelType {
    /// Integer code expected by new-api's `?type=` query.
    pub fn as_int(self) -> u8 {
        match self {
            ChannelType::Llm => 1,
            ChannelType::Embedding => 2,
            ChannelType::Tts => 8,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Channel {
    pub id: u64,
    pub name: String,
    #[serde(rename = "type")]
    pub channel_type: i32,
    pub status: i32,
    pub models: String,
    #[serde(default)]
    pub group: String,
    #[serde(default)]
    pub priority: Option<i32>,
    #[serde(default)]
    pub used_quota: Option<i64>,
    #[serde(default)]
    pub remain_quota: Option<i64>,
    #[serde(default)]
    pub test_time: Option<i64>,
    #[serde(default)]
    pub response_time: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct User {
    pub id: u64,
    pub username: String,
    #[serde(default)]
    pub display_name: Option<String>,
    pub role: i32,
    pub status: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProbeResult {
    pub base_url: String,
    pub user: User,
    pub server_version: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TestResult {
    pub status: u16,
    pub latency_ms: u128,
    pub model: Option<String>,
}
