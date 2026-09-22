"use client";
/**
 * A prominent, always-visible "find a person" search bar — NOT a dropdown
 * picker. Designed to be dropped into Reports, Website Usage, and Activity
 * Logs (each of those pages owns its own fetch + `smartSort`/`smartScore`
 * ranking and passes the resulting `results` in as a controlled prop).
 *
 * The two-line result row (name, then email + a disambiguating detail such as
 * device or role) mirrors the impersonation picker in ProfileMenu.tsx, which
 * solves the same "two people share a name" problem.
 */
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Search, X, Loader2 } from "lucide-react";

export interface TalentSearchResult {
  id: string;
  /** Line 1 of the result row. */
  name: string;
  /** Line 2, alongside `detail`. */
  email: string;
  /** Device hostname, role name, or similar — shown after the email on line 2
   *  to disambiguate people who share a name. */
  detail?: string | null;
}

export interface TalentSearchBarProps {
  value: string;
  onChange: (value: string) => void;
  /** Ranked results to display — the caller filters/sorts (e.g. with
   *  `smartSort` from `@/lib/smartSearch`) before passing them in. */
  results: TalentSearchResult[];
  onSelect: (result: TalentSearchResult) => void;
  loading?: boolean;
  placeholder?: string;
  /** Accessible label for the input (not shown visually; use `placeholder`
   *  for the visible hint). */
  label?: string;
  className?: string;
  autoFocus?: boolean;
}

export default function TalentSearchBar({
  value,
  onChange,
  results,
  onSelect,
  loading = false,
  placeholder = "Find a person by name or email…",
  label = "Find a person",
  className = "",
  autoFocus = false,
}: TalentSearchBarProps) {
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const reactId = useId();
  const inputId = `talent-search-${reactId}`;
  const listboxId = `talent-search-listbox-${reactId}`;
  const optionId = (id: string) => `talent-search-option-${reactId}-${id}`;

  // A stale highlighted index from the previous query must never point at the
  // wrong row once the result set changes.
  useEffect(() => {
    setHighlighted(results.length > 0 ? 0 : -1);
  }, [results]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  function handleSelect(result: TalentSearchResult) {
    onSelect(result);
    setOpen(false);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      if (results.length > 0) setHighlighted(i => (i + 1) % results.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      if (results.length > 0) setHighlighted(i => (i - 1 + results.length) % results.length);
    } else if (e.key === "Enter") {
      if (open && highlighted >= 0 && highlighted < results.length) {
        e.preventDefault();
        handleSelect(results[highlighted]);
      }
    } else if (e.key === "Escape") {
      if (value) {
        e.preventDefault();
        onChange("");
      } else {
        setOpen(false);
        inputRef.current?.blur();
      }
    }
  }

  const trimmed = value.trim();
  const showEmpty = open && !loading && trimmed === "";
  const showNoResults = open && !loading && trimmed !== "" && results.length === 0;
  const showResults = open && !loading && results.length > 0;
  const dropdownVisible = open && (loading || showEmpty || showNoResults || showResults);

  return (
    <div ref={wrapRef} className={`relative ${className}`}>
      <label htmlFor={inputId} className="sr-only">{label}</label>
      <div className="relative">
        <Search size={15} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
        <input
          ref={inputRef}
          id={inputId}
          type="text"
          role="combobox"
          aria-label={label}
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-controls={showResults ? listboxId : undefined}
          aria-autocomplete="list"
          aria-activedescendant={
            highlighted >= 0 && results[highlighted] ? optionId(results[highlighted].id) : undefined
          }
          autoComplete="off"
          autoFocus={autoFocus}
          value={value}
          onChange={e => { onChange(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className="w-full pl-11 pr-10 py-2.5 bg-white border border-gray-200 rounded-xl text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 shadow-sm"
        />
        {value && (
          <button
            type="button"
            onClick={() => { onChange(""); inputRef.current?.focus(); }}
            aria-label="Clear search"
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {dropdownVisible && (
        <div className="absolute left-0 right-0 mt-1.5 bg-white border border-gray-200 rounded-xl shadow-lg z-50 overflow-hidden">
          {loading && (
            <p className="px-4 py-6 text-center text-xs text-gray-500 flex items-center justify-center gap-2">
              <Loader2 size={13} className="animate-spin" /> Searching…
            </p>
          )}

          {showEmpty && (
            <p className="px-4 py-6 text-center text-xs text-gray-500">
              Start typing a name or email to find someone.
            </p>
          )}

          {showNoResults && (
            <div className="px-4 py-6 text-center">
              <p className="text-xs text-gray-600">No matches for &ldquo;{value}&rdquo;.</p>
              <button
                type="button"
                onClick={() => onChange("")}
                className="mt-2 text-xs font-medium text-brand-600 hover:text-brand-700"
              >
                Clear search
              </button>
            </div>
          )}

          {showResults && (
            <ul role="listbox" id={listboxId} aria-label={label} className="max-h-72 overflow-y-auto py-1">
              {results.map((r, i) => (
                <li key={r.id} id={optionId(r.id)} role="option" aria-selected={highlighted === i}>
                  <button
                    type="button"
                    onMouseDown={e => e.preventDefault()}
                    onClick={() => handleSelect(r)}
                    onMouseEnter={() => setHighlighted(i)}
                    className={`w-full text-left px-4 py-2 transition ${
                      highlighted === i ? "bg-brand-50" : "hover:bg-gray-50"
                    }`}
                  >
                    <span className="block text-sm font-medium text-gray-800 truncate">{r.name}</span>
                    <span className="block text-xs text-gray-500 truncate">
                      {r.email}
                      {r.detail ? ` · ${r.detail}` : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
