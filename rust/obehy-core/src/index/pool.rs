//! Interned IDs: every ID the index holds is one shared, reference-counted string per release.
//!
//! A location ID appears in every call at that location, a service ID in every trip and
//! `trip_services` entry, a namespace in every key: the pool stores each text once and hands
//! out [`Id`]s (a pointer and a length) that compare, hash and order by their text, exactly as
//! Python's `str` does. Unlike integer symbols an `Id` reads without the index, and holders
//! outside the index (journeys, plans) keep theirs valid after a sweep.
//!
//! Eviction: [`Pool::sweep`] drops every text nothing outside the pool holds any more; the
//! index calls it at the end of its own sweep.

use rustc_hash::FxHashSet as HashSet;
use std::sync::Arc;

/// An interned ID: compares, hashes and orders by its text (UTF-8 byte order = Python's code
/// point order).
pub type Id = Arc<str>;

/// The intern pool of one index.
#[derive(Debug, Default)]
pub struct Pool {
    texts: HashSet<Id>,
}

impl Pool {
    /// The shared ID for `text`.
    pub fn id(&mut self, text: &str) -> Id {
        if let Some(found) = self.texts.get(text) {
            return found.clone();
        }
        let id: Id = Arc::from(text);
        self.texts.insert(id.clone());
        id
    }

    /// `id` for an optional text.
    pub fn opt(&mut self, text: Option<&str>) -> Option<Id> {
        text.map(|t| self.id(t))
    }

    /// Drop the texts only the pool still holds, and give the freed capacity back.
    pub fn sweep(&mut self) {
        self.texts.retain(|id| Arc::strong_count(id) > 1);
        self.texts.shrink_to_fit();
    }

    pub fn len(&self) -> usize {
        self.texts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.texts.is_empty()
    }

    /// Bytes held: the set's slots and the texts' allocations (two reference counts each).
    pub fn heap_bytes(&self) -> usize {
        self.texts.capacity() * (std::mem::size_of::<Id>() + 1)
            + self.texts.iter().map(|t| t.len() + 16).sum::<usize>()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_text_one_allocation_and_unheld_texts_leave() {
        let mut pool = Pool::default();
        let a = pool.id("jdf:stop:1");
        let b = pool.id("jdf:stop:1");
        assert!(Arc::ptr_eq(&a, &b));
        {
            let _gone = pool.id("jdf:stop:2");
        }
        assert_eq!(pool.len(), 2);
        pool.sweep();
        assert_eq!(pool.len(), 1);
        assert!(Arc::ptr_eq(&pool.id("jdf:stop:1"), &a));
    }
}
