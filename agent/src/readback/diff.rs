//! Locate pasted text inside a field's current contents and extract the word-level
//! substitutions the user made. Pure logic, unit-tested; no platform code.

/// One correction the user typed over pasted text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Correction {
    pub heard: String,
    pub meant: String,
}

/// Outcome of comparing what we pasted with what the field holds now.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Observation {
    /// The pasted text is no longer findable (deleted, field cleared, focus elsewhere).
    Gone,
    /// The pasted region is unchanged.
    Unchanged,
    /// The user edited the region; these are the word substitutions worth learning.
    Edited { corrections: Vec<Correction>, region_now: String },
}

fn norm(w: &str) -> String {
    w.trim_matches(|c: char| !c.is_alphanumeric() && c != '\'' && c != '-')
        .to_lowercase()
}

fn words(s: &str) -> Vec<String> {
    s.split_whitespace().map(|w| w.to_string()).collect()
}

/// Find where `pasted` sits in `current`, allowing edits: returns the window of
/// `current` words that best matches, or None if fewer than half the words match.
fn locate<'a>(pasted_n: &[String], current_w: &'a [String]) -> Option<(usize, usize)> {
    if pasted_n.is_empty() || current_w.is_empty() {
        return None;
    }
    let current_n: Vec<String> = current_w.iter().map(|w| norm(w)).collect();
    // Anchor on the first and last three normalized words; scan candidate starts.
    let n = pasted_n.len();
    let mut best: Option<(usize, usize, usize)> = None; // (start, end, matched)
    let max_len = (n * 3 / 2 + 3).max(n);
    for start in 0..current_n.len() {
        // Quick reject: first word should appear near `start`.
        let head_ok = (start..(start + 2).min(current_n.len())).any(|k| current_n[k] == pasted_n[0]);
        if !head_ok && n > 2 {
            continue;
        }
        for len in (n.saturating_sub(n / 2 + 1).max(1))..=max_len.min(current_n.len() - start) {
            let window = &current_n[start..start + len];
            let matched = lcs_len(pasted_n, window);
            if matched * 2 >= n && best.map(|b| matched > b.2 || (matched == b.2 && len < b.1 - b.0)).unwrap_or(true) {
                best = Some((start, start + len, matched));
            }
        }
    }
    best.map(|(s, e, _)| (s, e))
}

fn lcs_len(a: &[String], b: &[String]) -> usize {
    let mut prev = vec![0usize; b.len() + 1];
    let mut cur = vec![0usize; b.len() + 1];
    for i in 1..=a.len() {
        for j in 1..=b.len() {
            cur[j] = if a[i - 1] == b[j - 1] { prev[j - 1] + 1 } else { prev[j].max(cur[j - 1]) };
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[b.len()]
}

/// Myers-free simple diff: LCS backtrack yielding (op, a_idx, b_idx) runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Op {
    Eq,
    Del,
    Ins,
}

fn diff_ops(a: &[String], b: &[String]) -> Vec<Op> {
    let (n, m) = (a.len(), b.len());
    let mut dp = vec![vec![0usize; m + 1]; n + 1];
    for i in (0..n).rev() {
        for j in (0..m).rev() {
            dp[i][j] = if a[i] == b[j] { dp[i + 1][j + 1] + 1 } else { dp[i + 1][j].max(dp[i][j + 1]) };
        }
    }
    let (mut i, mut j) = (0, 0);
    let mut ops = Vec::new();
    while i < n && j < m {
        if a[i] == b[j] {
            ops.push(Op::Eq);
            i += 1;
            j += 1;
        } else if dp[i + 1][j] >= dp[i][j + 1] {
            ops.push(Op::Del);
            i += 1;
        } else {
            ops.push(Op::Ins);
            j += 1;
        }
    }
    ops.extend(std::iter::repeat_n(Op::Del, n - i));
    ops.extend(std::iter::repeat_n(Op::Ins, m - j));
    ops
}

/// Compare the pasted text with the field's current contents.
pub fn observe(pasted: &str, current: &str) -> Observation {
    let pasted_w = words(pasted);
    let pasted_n: Vec<String> = pasted_w.iter().map(|w| norm(w)).filter(|w| !w.is_empty()).collect();
    let current_w = words(current);
    let Some((s, e)) = locate(&pasted_n, &current_w) else { return Observation::Gone };
    let region: Vec<String> = current_w[s..e].to_vec();
    let region_n: Vec<String> = region.iter().map(|w| norm(w)).collect();
    if region_n == pasted_n {
        return Observation::Unchanged;
    }
    // Walk the diff, grouping adjacent Del/Ins runs into substitutions.
    let ops = diff_ops(&pasted_n, &region_n);
    let (mut i, mut j) = (0usize, 0usize);
    let mut corrections = Vec::new();
    let mut k = 0;
    while k < ops.len() {
        match ops[k] {
            Op::Eq => {
                i += 1;
                j += 1;
                k += 1;
            }
            _ => {
                let mut dels = Vec::new();
                let mut ins = Vec::new();
                while k < ops.len() && ops[k] != Op::Eq {
                    match ops[k] {
                        Op::Del => {
                            dels.push(pasted_n[i].clone());
                            i += 1;
                        }
                        Op::Ins => {
                            ins.push(region[j].clone());
                            j += 1;
                        }
                        Op::Eq => unreachable!(),
                    }
                    k += 1;
                }
                // Only substitutions are corrections; pure insertions/deletions are the
                // user writing more or trimming, not a mishearing. Keep them short.
                if !dels.is_empty() && !ins.is_empty() && dels.len() <= 3 && ins.len() <= 3 {
                    let heard = dels.join(" ");
                    let meant = ins.iter().map(|w| w.trim_matches(|c: char| !c.is_alphanumeric() && c != '\'' && c != '-')).collect::<Vec<_>>().join(" ");
                    if !meant.is_empty() && norm(&meant) != heard {
                        corrections.push(Correction { heard, meant });
                    }
                }
            }
        }
    }
    Observation::Edited { corrections, region_now: region.join(" ") }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unchanged_paste_inside_longer_text() {
        let pasted = "deploy it to cooper netties on friday";
        let current = "Hi team. Deploy it to cooper netties on Friday. Thanks!";
        assert_eq!(observe(pasted, current), Observation::Unchanged);
    }

    #[test]
    fn substitution_is_a_correction() {
        let pasted = "deploy it to cooper netties on friday";
        let current = "Hi team. Deploy it to Kubernetes on Friday. Thanks!";
        match observe(pasted, current) {
            Observation::Edited { corrections, .. } => {
                assert_eq!(corrections, vec![Correction { heard: "cooper netties".into(), meant: "Kubernetes".into() }]);
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn name_fix_single_word() {
        let pasted = "ask mark about the api";
        let current = "ask Marc about the API";
        match observe(pasted, current) {
            Observation::Edited { corrections, .. } => {
                assert_eq!(corrections, vec![Correction { heard: "mark".into(), meant: "Marc".into() }]);
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn user_appending_more_text_is_not_a_correction() {
        let pasted = "send the report";
        let current = "send the report tomorrow morning please";
        assert_eq!(observe(pasted, current), Observation::Unchanged);
    }

    #[test]
    fn deleted_paste_is_gone() {
        assert_eq!(observe("send the report", "something else entirely here"), Observation::Gone);
        assert_eq!(observe("send the report", ""), Observation::Gone);
    }

    #[test]
    fn punctuation_and_case_only_changes_are_ignored() {
        let pasted = "never merge this before friday";
        let current = "Never merge this before Friday.";
        assert_eq!(observe(pasted, current), Observation::Unchanged);
    }

    #[test]
    fn devanagari_correction() {
        let pasted = "कल की meeting में हमने decide किया";
        let current = "कल की मीटिंग में हमने decide किया";
        match observe(pasted, current) {
            Observation::Edited { corrections, .. } => {
                assert_eq!(corrections, vec![Correction { heard: "meeting".into(), meant: "मीटिंग".into() }]);
            }
            other => panic!("{other:?}"),
        }
    }
}
