//! CPython 3.13's `html.parser.HTMLParser` with `convert_charrefs=True`, fed a whole document
//! at once (`feed(text)` then `close()`), as the SŽ board decoder uses it (decode spec §4.4).
//!
//! Ported statement for statement from `html/parser.py` (the regular expressions written out
//! as scanners, their backtracking included) and `html.unescape` with the HTML5 entity table.
//! Only what a handler sees is produced: start tags (with attributes), end tags and text;
//! comments, declarations and processing instructions are parsed and dropped.

use std::collections::HashMap;
use std::sync::OnceLock;

use super::html_entities::{HTML5, INVALID_CHARREFS, INVALID_CODEPOINTS};

/// What the parser reports, in document order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A start tag: the lower-cased name and the attributes in order (None: no value).
    Start(String, Vec<(String, Option<String>)>),
    End(String),
    Data(String),
}

/// The events of a whole document.
pub fn parse(text: &str) -> Vec<Event> {
    let mut parser = Parser {
        raw: text.chars().collect(),
        events: Vec::new(),
        cdata_elem: None,
        escapable: true,
    };
    if !parser.raw.is_empty() {
        parser.goahead(false);
    }
    parser.goahead(true);
    parser.events
}

struct Parser {
    raw: Vec<char>,
    events: Vec<Event>,
    cdata_elem: Option<String>,
    escapable: bool,
}

const CDATA_CONTENT_ELEMENTS: [&str; 6] =
    ["script", "style", "xmp", "iframe", "noembed", "noframes"];
const RCDATA_CONTENT_ELEMENTS: [&str; 2] = ["textarea", "title"];

fn ws(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\r' | '\u{c}' | ' ')
}

fn starts(raw: &[char], at: usize, prefix: &str) -> bool {
    (at..)
        .zip(prefix.chars())
        .all(|(i, p)| raw.get(i) == Some(&p))
}

fn find(raw: &[char], c: char, from: usize) -> Option<usize> {
    raw.get(from..)?
        .iter()
        .position(|&x| x == c)
        .map(|p| p + from)
}

fn text(raw: &[char]) -> String {
    raw.iter().collect()
}

impl Parser {
    fn data(&mut self, data: String) {
        self.events.push(Event::Data(data));
    }

    fn goahead(&mut self, end: bool) {
        let raw = std::mem::take(&mut self.raw);
        let n = raw.len();
        let mut i = 0;
        while i < n {
            let j;
            match &self.cdata_elem {
                None => match find(&raw, '<', i) {
                    Some(p) => j = p,
                    None => {
                        let lo = i.max(n.saturating_sub(34));
                        let amppos = raw[lo..n].iter().rposition(|&c| c == '&').map(|p| p + lo);
                        if let Some(a) = amppos
                            && !raw[a..n].iter().any(|&c| ws(c) || c == ';')
                        {
                            break;
                        }
                        j = n;
                    }
                },
                Some(elem) => {
                    if elem == "plaintext" {
                        j = n;
                    } else if let Some(p) = cdata_end(&raw, i, elem) {
                        j = p;
                    } else {
                        break;
                    }
                }
            }
            if i < j {
                let chunk = text(&raw[i..j]);
                let chunk = if self.escapable {
                    unescape(&chunk)
                } else {
                    chunk
                };
                self.data(chunk);
            }
            i = j;
            if i == n {
                break;
            }
            // `interesting` only ever stops at '<' here (convert_charrefs).
            let next_letter = raw.get(i + 1).is_some_and(char::is_ascii_alphabetic);
            let k: isize = if next_letter {
                self.parse_starttag(&raw, i)
            } else if starts(&raw, i, "</") {
                self.parse_endtag(&raw, i)
            } else if starts(&raw, i, "<!--") {
                parse_comment(&raw, i)
            } else if starts(&raw, i, "<?") {
                find(&raw, '>', i + 2).map_or(-1, |p| (p + 1) as isize)
            } else if starts(&raw, i, "<!") {
                parse_html_declaration(&raw, i)
            } else if i + 1 < n || end {
                self.data("<".into());
                (i + 1) as isize
            } else {
                break;
            };
            let k = if k < 0 {
                if !end {
                    break;
                }
                if starts(&raw, i, "</") && i + 2 == n {
                    self.data("</".into());
                }
                // Everything else at the end is a dropped tag, comment or declaration.
                n
            } else {
                k as usize
            };
            i = k;
        }
        if end && i < n {
            let chunk = text(&raw[i..n]);
            let chunk = if self.escapable {
                unescape(&chunk)
            } else {
                chunk
            };
            self.data(chunk);
            i = n;
        }
        self.raw = raw[i..].to_vec();
    }

    fn parse_starttag(&mut self, raw: &[char], i: usize) -> isize {
        let endpos = locatetagend(raw, i + 1);
        if raw[endpos - 1] != '>' {
            return -1;
        }
        let (name_end, mut k) = tagfind(raw, i + 1);
        let tag = text(&raw[i + 1..name_end]).to_lowercase();
        let mut attrs = Vec::new();
        while k < endpos {
            let Some((name, value, next)) = attrfind(raw, k) else {
                break;
            };
            let value = value.map(|(a, b)| {
                let mut v = text(&raw[a..b]);
                let quoted = (v.starts_with('\'') && v.ends_with('\'') && !v.is_empty())
                    || (v.starts_with('"') && v.ends_with('"') && !v.is_empty());
                if quoted {
                    v = v
                        .chars()
                        .skip(1)
                        .take(v.chars().count().saturating_sub(2))
                        .collect();
                }
                if v.is_empty() { v } else { unescape_attr(&v) }
            });
            attrs.push((text(&raw[name.0..name.1]).to_lowercase(), value));
            k = next;
        }
        let rest = text(&raw[k..endpos]);
        let rest = obehy_core::py::text::strip(&rest);
        if rest != ">" && rest != "/>" {
            self.data(text(&raw[i..endpos]));
            return endpos as isize;
        }
        if rest.ends_with("/>") {
            self.events.push(Event::Start(tag.clone(), attrs));
            self.events.push(Event::End(tag));
        } else {
            self.events.push(Event::Start(tag.clone(), attrs));
            if CDATA_CONTENT_ELEMENTS.contains(&tag.as_str()) || tag == "plaintext" {
                self.cdata_elem = Some(tag);
                self.escapable = false;
            } else if RCDATA_CONTENT_ELEMENTS.contains(&tag.as_str()) {
                self.cdata_elem = Some(tag);
                self.escapable = true;
            }
        }
        endpos as isize
    }

    fn parse_endtag(&mut self, raw: &[char], i: usize) -> isize {
        if find(raw, '>', i + 2).is_none() {
            return -1;
        }
        if !raw.get(i + 2).is_some_and(char::is_ascii_alphabetic) {
            if raw.get(i + 2) == Some(&'>') {
                return (i + 3) as isize;
            }
            return find(raw, '>', i + 2).map_or(-1, |p| (p + 1) as isize);
        }
        let j = locatetagend(raw, i + 2);
        if raw[j - 1] != '>' {
            return -1;
        }
        let (name_end, _) = tagfind(raw, i + 2);
        self.events
            .push(Event::End(text(&raw[i + 2..name_end]).to_lowercase()));
        self.cdata_elem = None;
        self.escapable = true;
        j as isize
    }
}

/// `</elem(?=[\t\n\r\f />])`, ASCII case-insensitive, searched from `from`.
fn cdata_end(raw: &[char], from: usize, elem: &str) -> Option<usize> {
    let name: Vec<char> = elem.chars().collect();
    let mut p = from;
    while p + 2 + name.len() < raw.len() {
        if raw[p] == '<'
            && raw[p + 1] == '/'
            && raw[p + 2..p + 2 + name.len()]
                .iter()
                .zip(&name)
                .all(|(a, b)| a.is_ascii() && a.eq_ignore_ascii_case(b) || a == b)
            && {
                let after = raw[p + 2 + name.len()];
                ws(after) || after == '/' || after == '>'
            }
        {
            return Some(p);
        }
        p += 1;
    }
    None
}

fn parse_comment(raw: &[char], i: usize) -> isize {
    // An empty comment is closed by the first ">" or "->".
    if raw.get(i + 4) == Some(&'>') {
        return (i + 5) as isize;
    }
    if starts(raw, i + 4, "->") {
        return (i + 6) as isize;
    }
    let mut p = i + 4;
    while p < raw.len() {
        if starts(raw, p, "-->") {
            return (p + 3) as isize;
        }
        if starts(raw, p, "--!>") {
            return (p + 4) as isize;
        }
        p += 1;
    }
    -1
}

fn parse_html_declaration(raw: &[char], i: usize) -> isize {
    if starts(raw, i, "<!--") {
        return parse_comment(raw, i);
    }
    if starts(raw, i, "<![CDATA[") {
        let mut p = i + 9;
        while p < raw.len() {
            if starts(raw, p, "]]>") {
                return (p + 3) as isize;
            }
            p += 1;
        }
        return -1;
    }
    let head = text(&raw[i..(i + 9).min(raw.len())]).to_lowercase();
    if head == "<!doctype" {
        return find(raw, '>', i + 9).map_or(-1, |p| (p + 1) as isize);
    }
    find(raw, '>', i + 2).map_or(-1, |p| (p + 1) as isize)
}

/// `tagfind_tolerant` at `p` (a letter): the name's end and the match's end.
fn tagfind(raw: &[char], p: usize) -> (usize, usize) {
    let n = raw.len();
    let mut q = p + 1;
    while q < n && !(ws(raw[q]) || raw[q] == '/' || raw[q] == '>') {
        q += 1;
    }
    let name_end = q;
    (name_end, skip_space_or_slash(raw, q))
}

/// `(?:[\t\n\r\f ]|/(?!>))*`
fn skip_space_or_slash(raw: &[char], mut q: usize) -> usize {
    while q < raw.len() && (ws(raw[q]) || (raw[q] == '/' && raw.get(q + 1) != Some(&'>'))) {
        q += 1;
    }
    q
}

/// The optional value group after an attribute name, with the regex's backtracking: the end
/// of the group and the value's span.
fn value_group(raw: &[char], q: usize) -> Option<(usize, (usize, usize))> {
    let n = raw.len();
    let mut r = q;
    while r < n && ws(raw[r]) {
        r += 1;
    }
    if r >= n || raw[r] != '=' {
        return None;
    }
    let after_eq = r + 1;
    let mut most = after_eq;
    while most < n && ws(raw[most]) {
        most += 1;
    }
    for pos in (after_eq..=most).rev() {
        let c = raw.get(pos).copied();
        if let Some(quote @ ('\'' | '"')) = c {
            if let Some(close) = find(raw, quote, pos + 1) {
                return Some((close + 1, (pos, close + 1)));
            }
            continue;
        }
        let mut e = pos;
        while e < n && !(ws(raw[e]) || raw[e] == '>') {
            e += 1;
        }
        return Some((e, (pos, e)));
    }
    None
}

fn attr_name_start(raw: &[char], q: usize) -> bool {
    q > 0
        && q < raw.len()
        && matches!(
            raw[q - 1],
            '\'' | '"' | '\t' | '\n' | '\r' | '\u{c}' | ' ' | '/'
        )
        && !(ws(raw[q]) || raw[q] == '/' || raw[q] == '>')
}

fn attr_name_end(raw: &[char], mut q: usize) -> usize {
    q += 1;
    while q < raw.len() && !(ws(raw[q]) || raw[q] == '/' || raw[q] == '=' || raw[q] == '>') {
        q += 1;
    }
    q
}

/// `attrfind_tolerant.match(rawdata, k)`: the name's span, the value's span (None without a
/// value) and the match's end.
type Attr = ((usize, usize), Option<(usize, usize)>, usize);

fn attrfind(raw: &[char], k: usize) -> Option<Attr> {
    if !attr_name_start(raw, k) {
        return None;
    }
    let name_end = attr_name_end(raw, k);
    let (q, value) = match value_group(raw, name_end) {
        Some((end, span)) => (end, Some(span)),
        None => (name_end, None),
    };
    Some(((k, name_end), value, skip_space_or_slash(raw, q)))
}

/// `locatetagend.match(rawdata, p)` (`p` at a letter): the match's end.
fn locatetagend(raw: &[char], p: usize) -> usize {
    let n = raw.len();
    let mut q = p + 1;
    while q < n && !(ws(raw[q]) || raw[q] == '/' || raw[q] == '>') {
        q += 1;
    }
    while q < n && (ws(raw[q]) || raw[q] == '/') {
        q += 1;
    }
    while attr_name_start(raw, q) {
        let name_end = attr_name_end(raw, q);
        q = value_group(raw, name_end).map_or(name_end, |(end, _)| end);
        while q < n && (ws(raw[q]) || raw[q] == '/') {
            q += 1;
        }
    }
    if q < n && raw[q] == '>' {
        q += 1;
    }
    q
}

fn html5() -> &'static HashMap<&'static str, &'static str> {
    static TABLE: OnceLock<HashMap<&'static str, &'static str>> = OnceLock::new();
    TABLE.get_or_init(|| HTML5.iter().copied().collect())
}

fn numeric(num: Option<u32>) -> String {
    let Some(num) = num.filter(|&n| n <= 0x10FFFF) else {
        return "\u{FFFD}".into();
    };
    if let Ok(i) = INVALID_CHARREFS.binary_search_by_key(&num, |&(k, _)| k) {
        return INVALID_CHARREFS[i].1.into();
    }
    if (0xD800..=0xDFFF).contains(&num) {
        return "\u{FFFD}".into();
    }
    if INVALID_CODEPOINTS.binary_search(&num).is_ok() {
        return String::new();
    }
    char::from_u32(num).map(String::from).unwrap_or_default()
}

fn named(s: &str) -> String {
    let table = html5();
    if let Some(found) = table.get(s) {
        return (*found).into();
    }
    let chars: Vec<char> = s.chars().collect();
    for x in (2..chars.len()).rev() {
        let head: String = chars[..x].iter().collect();
        if let Some(found) = table.get(head.as_str()) {
            let tail: String = chars[x..].iter().collect();
            return format!("{found}{tail}");
        }
    }
    format!("&{s}")
}

fn digits_value(digits: &[char], radix: u32) -> Option<u32> {
    let mut value: u64 = 0;
    for c in digits {
        value = value * u64::from(radix) + u64::from(c.to_digit(radix)?);
        if value > 0x10FFFF {
            return None;
        }
    }
    u32::try_from(value).ok()
}

/// `html.unescape`.
pub fn unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_owned();
    }
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < n {
        if chars[i] != '&' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let mut j = i + 1;
        if chars.get(j) == Some(&'#') {
            j += 1;
            let hex = matches!(chars.get(j), Some('x' | 'X'))
                && chars.get(j + 1).is_some_and(char::is_ascii_hexdigit);
            if chars.get(j).is_some_and(char::is_ascii_digit) {
                let start = j;
                while j < n && chars[j].is_ascii_digit() {
                    j += 1;
                }
                let value = digits_value(&chars[start..j], 10);
                if chars.get(j) == Some(&';') {
                    j += 1;
                }
                out.push_str(&numeric(value));
                i = j;
            } else if hex {
                let start = j + 1;
                j = start;
                while j < n && chars[j].is_ascii_hexdigit() {
                    j += 1;
                }
                let value = digits_value(&chars[start..j], 16);
                if chars.get(j) == Some(&';') {
                    j += 1;
                }
                out.push_str(&numeric(value));
                i = j;
            } else {
                out.push('&');
                i += 1;
            }
            continue;
        }
        let start = j;
        while j < n
            && j - start < 32
            && !matches!(
                chars[j],
                '\t' | '\n' | '\u{c}' | ' ' | '<' | '&' | '#' | ';'
            )
        {
            j += 1;
        }
        if j == start {
            out.push('&');
            i += 1;
            continue;
        }
        if chars.get(j) == Some(&';') {
            j += 1;
        }
        let name: String = chars[start..j].iter().collect();
        out.push_str(&named(&name));
        i = j;
    }
    out
}

/// `_unescape_attrvalue`: references in an attribute value.
fn unescape_attr(s: &str) -> String {
    if !s.contains('&') {
        return s.to_owned();
    }
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < n {
        if chars[i] != '&' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let mut j = i + 1;
        let matched = if chars.get(j) == Some(&'#') {
            if chars.get(j + 1).is_some_and(char::is_ascii_digit) {
                j += 1;
                while j < n && chars[j].is_ascii_digit() {
                    j += 1;
                }
                true
            } else if matches!(chars.get(j + 1), Some('x' | 'X'))
                && chars.get(j + 2).is_some_and(char::is_ascii_hexdigit)
            {
                j += 2;
                while j < n && chars[j].is_ascii_hexdigit() {
                    j += 1;
                }
                true
            } else {
                false
            }
        } else if chars.get(j).is_some_and(char::is_ascii_alphabetic) {
            while j < n && chars[j].is_ascii_alphanumeric() {
                j += 1;
            }
            true
        } else {
            false
        };
        if !matched {
            out.push('&');
            i += 1;
            continue;
        }
        if matches!(chars.get(j), Some(';' | '=')) {
            j += 1;
        }
        let reference: String = chars[i..j].iter().collect();
        let numeric = reference.starts_with("&#");
        let named = !reference.ends_with('=') && html5().contains_key(&reference[1..]);
        if numeric || named {
            out.push_str(&unescape(&reference));
        } else {
            out.push_str(&reference);
        }
        i = j;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unescapes_like_python() {
        assert_eq!(
            unescape("a&nbsp;b &amp &ampx &#65;&#x42 &#0; &#128; &bogus;"),
            "a\u{a0}b & &x AB \u{FFFD} \u{20AC} &bogus;"
        );
        assert_eq!(unescape_attr("a&amp;b&amp=c&ampx"), "a&b&amp=c&ampx");
    }

    #[test]
    fn parses_tags_text_and_raw_text() {
        let events =
            parse("<DIV class='a b' x data-y=\"1\"><td>x&lt;y</td><script>a<b</script><br/>t");
        assert_eq!(
            events,
            vec![
                Event::Start(
                    "div".into(),
                    vec![
                        ("class".into(), Some("a b".into())),
                        ("x".into(), None),
                        ("data-y".into(), Some("1".into()))
                    ]
                ),
                Event::Start("td".into(), vec![]),
                Event::Data("x<y".into()),
                Event::End("td".into()),
                Event::Start("script".into(), vec![]),
                Event::Data("a<b".into()),
                Event::End("script".into()),
                Event::Start("br".into(), vec![]),
                Event::End("br".into()),
                Event::Data("t".into()),
            ]
        );
    }
}
