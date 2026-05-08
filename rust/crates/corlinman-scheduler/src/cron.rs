//! Cron expression parsing helpers.
//!
//! We re-use the `cron` crate (already a workspace dep) for the 7-field
//! format `sec min hour day month weekday year`. The rest of the codebase
//! (gateway admin, `doctor` checks) was already written against this
//! crate's grammar, so we don't introduce a second dialect here.

use std::str::FromStr;

use chrono::{DateTime, Utc};
pub use cron::Schedule;

/// Parse a cron expression. Thin wrapper that exists so callers don't
/// have to import `cron::Schedule` + `std::str::FromStr` directly.
pub fn parse(expr: &str) -> Result<Schedule, cron::error::Error> {
    Schedule::from_str(expr)
}

/// Compute the *next* trigger strictly after `now`. Returns `None` if the
/// schedule has no upcoming firing (e.g. invalid DOM/month combo); callers
/// should treat that as "this job will never fire" and bail out of the
/// per-job tick loop rather than busy-looping on `None`.
pub fn next_after(schedule: &Schedule, now: DateTime<Utc>) -> Option<DateTime<Utc>> {
    schedule.after(&now).next()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_seven_field_cron() {
        let s = parse("0 0 3 * * * *").expect("daily 3am parses");
        let now = Utc::now();
        assert!(
            next_after(&s, now).is_some(),
            "daily cron should have a next"
        );
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse("not a cron").is_err());
    }
}
