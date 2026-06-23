// fabri polyglot example: a regex-over-lines tool, in Rust.
//
// Demonstrates the fabri tool contract from a non-Python language:
//   1. Read one JSON object from stdin.
//   2. Print one JSON object to stdout.
//   3. Exit 0 on success, non-zero on failure (the runner wraps the body in
//      the standard `{ok, error?, result?}` envelope either way).
//
// The tool greps a file for a regex pattern and returns the matching lines.
// Realistic enough to be useful; small enough to read in one sitting.

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{self, Read};
use std::process::ExitCode;

#[derive(Deserialize)]
struct Args {
    path: String,
    pattern: String,
    #[serde(default = "default_max")]
    max_matches: usize,
}

fn default_max() -> usize { 100 }

#[derive(Serialize)]
struct Match {
    line: usize,
    text: String,
}

#[derive(Serialize)]
struct Output {
    path: String,
    pattern: String,
    matches: Vec<Match>,
    truncated: bool,
}

#[derive(Serialize)]
struct Error {
    error: String,
}

fn run() -> Result<Output, String> {
    let mut buf = String::new();
    io::stdin().read_to_string(&mut buf).map_err(|e| format!("read stdin: {e}"))?;
    let args: Args = serde_json::from_str(&buf).map_err(|e| format!("parse args: {e}"))?;
    let re = Regex::new(&args.pattern).map_err(|e| format!("invalid regex: {e}"))?;
    let content = fs::read_to_string(&args.path).map_err(|e| format!("read {}: {e}", args.path))?;

    let mut matches = Vec::new();
    let mut truncated = false;
    for (i, line) in content.lines().enumerate() {
        if re.is_match(line) {
            if matches.len() >= args.max_matches {
                truncated = true;
                break;
            }
            matches.push(Match { line: i + 1, text: line.to_string() });
        }
    }
    Ok(Output { path: args.path, pattern: args.pattern, matches, truncated })
}

fn main() -> ExitCode {
    match run() {
        Ok(out) => {
            println!("{}", serde_json::to_string(&out).unwrap());
            ExitCode::SUCCESS
        }
        Err(msg) => {
            println!("{}", serde_json::to_string(&Error { error: msg }).unwrap());
            ExitCode::from(1)
        }
    }
}
