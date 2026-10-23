// Copyright (c) 2014-2017 Guillaume Pinot <texitoi(a)texitoi.eu>
//
// This work is free. You can redistribute it and/or modify it under
// the terms of the Do What The Fuck You Want To Public License,
// Version 2, as published by Sam Hocevar. See the COPYING file for
// more details.

/// Abstract behind a tuple struct an iterator.  Usefull to privatize
/// implementation details about the implementation of the iterator.
/// The syntax is done to be as if you write a type definition.
///
/// First you put the (non optional) doc inside `#[doc="..."]`. Then
/// the name of your type with its generic parameter between
/// `[]`. After `=` you put the real type that should be hidden, with
/// an optional `where` clause.
///
/// # Example
///
/// ```
/// // In the crate root module:
/// #[macro_use] extern crate pub_iterator_type;
///
/// // Declare the type
/// pub_iterator_type! {
///     #[doc="An iterator that yield infinitelly the default value."]
///     RepeatDefault[T] = std::iter::Repeat<T> where T: Default + Clone
/// }
/// pub fn repeat_default<T: Default + Clone>() -> RepeatDefault<T> {
///     RepeatDefault(std::iter::repeat(T::default()))
/// }
///
/// # fn main() {
/// let iter = repeat_default::<i32>();
/// for i in iter.take(100) {
///     assert_eq!(0, i);
/// }
/// # }
/// ```
#[macro_export]
macro_rules! pub_iterator_type {
    ( #[$($attr:tt)*] $Name:ident [ $($NameParam:tt)* ] = $From:ty ) => {
        #[$($attr)*]
        #[must_use = "iterator adaptors are lazy and do nothing unless consumed"]
        pub struct $Name < $($NameParam)* > ( $From );
        impl< $($NameParam)* > Iterator for $Name < $($NameParam)* > {
            type Item = < $From as Iterator>::Item;
            fn next(&mut self) -> Option<Self::Item> {
                self.0.next()
            }
            fn size_hint(&self) -> (usize, Option<usize>) {
                self.0.size_hint()
            }
        }
    };
    ( #[$($attr:tt)*] $Name:ident [ $($NameParam:tt)* ] = $From:ty where $($w:tt)* ) => {
        #[$($attr)*]
        #[must_use = "iterator adaptors are lazy and do nothing unless consumed"]
        pub struct $Name < $($NameParam)* > ( $From ) where $($w)* ;
        impl< $($NameParam)* > Iterator for $Name < $($NameParam)* > where $($w)* {
            type Item = <$From as Iterator>::Item;
            fn next(&mut self) -> Option<Self::Item> {
                self.0.next()
            }
            fn size_hint(&self) -> (usize, Option<usize>) {
                self.0.size_hint()
            }
        }
    }
}
