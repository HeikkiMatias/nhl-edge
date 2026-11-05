"use client";

// The dashboard's shared pieces (#398, #399): the tabs, which follow the URL's hash so each is
// linkable and survives a reload; the ⓘ help pop-up; the theme switch; and a segmented picker.

import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { GLOSSARY } from "@/lib/glossary";
import { HELP, type HelpKey } from "@/lib/help";
import { THEME_KEY } from "@/lib/theme";

export type Tab = { id: string; label: string };

/** The hash's path, "#betting/results" as ["betting", "results"], followed as it changes. */
export function useHash(): [string[], (path: string[]) => void] {
  const read = () =>
    typeof window === "undefined" ? [] : window.location.hash.replace(/^#/, "").split("/").filter(Boolean);
  const [path, setPath] = useState<string[]>(read);
  useEffect(() => {
    const changed = () => setPath(read());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const go = (next: string[]) => {
    // replaceState keeps the back button for leaving the page, not for every tab.
    window.history.replaceState(null, "", `#${next.join("/")}`);
    setPath(next);
  };
  return [path, go];
}

/** A tab bar (role tablist): click or the arrow keys, Home and End move between tabs. */
export function Tabs({
  tabs,
  active,
  onSelect,
  label,
  small = false,
}: {
  tabs: Tab[];
  active: string;
  onSelect: (id: string) => void;
  label: string;
  small?: boolean;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const key = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = tabs.length - 1;
    const next = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: last }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    const to = (next + tabs.length) % tabs.length;
    onSelect(tabs[to].id);
    refs.current[to]?.focus();
  };
  return (
    <div className={small ? "tabs sub" : "tabs"} role="tablist" aria-label={label}>
      {tabs.map((t, i) => (
        <button
          key={t.id}
          ref={(node) => {
            refs.current[i] = node;
          }}
          type="button"
          role="tab"
          id={`tab-${t.id}`}
          aria-selected={t.id === active}
          aria-controls={`panel-${t.id}`}
          tabIndex={t.id === active ? 0 : -1}
          onClick={() => onSelect(t.id)}
          onKeyDown={(e) => key(e, i)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Panel({ id, children }: { id: string; children: ReactNode }) {
  return (
    <section role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`}>
      {children}
    </section>
  );
}

/** An ⓘ button whose pop-up says what a section shows, how to read it, where it comes from,
 * and the glossary's words for its terms. It closes on Esc, a click outside or its button, and
 * gives the focus back to the ⓘ. */
export function Info({ topic }: { topic: HelpKey }) {
  const help = HELP[topic];
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    // A browser shows it modally; where showModal doesn't exist (jsdom), as an open dialog.
    if (open && !node.hasAttribute("open")) {
      if (typeof node.showModal === "function") node.showModal();
      else node.setAttribute("open", "");
    } else if (!open && node.hasAttribute("open")) {
      if (typeof node.close === "function") node.close();
      else node.removeAttribute("open");
      button.current?.focus();
    }
  }, [open]);
  return (
    <>
      <button
        ref={button}
        type="button"
        className="info"
        aria-label={`About: ${help.title}`}
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        ⓘ
      </button>
      <dialog
        ref={dialog}
        className="help"
        aria-labelledby={id}
        onClose={() => setOpen(false)}
        onCancel={(e) => {
          e.preventDefault();
          setOpen(false);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            setOpen(false);
          }
        }}
        // A click on the backdrop lands on the dialog itself, outside its content.
        onClick={(e) => {
          if (e.target === e.currentTarget) setOpen(false);
        }}
      >
        {open ? (
          <div className="help-body">
            <h3 id={id}>{help.title}</h3>
            <p>
              <strong>What it shows.</strong> {help.what}
            </p>
            <p>
              <strong>How to read it.</strong> {help.read}
            </p>
            <p className="muted">From: {help.source}</p>
            {help.terms.length ? (
              <dl>
                {help.terms.map((t) => (
                  <div key={t}>
                    <dt>{GLOSSARY[t].term}</dt>
                    <dd>{GLOSSARY[t].meaning}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            <button type="button" className="secondary" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>
        ) : null}
      </dialog>
    </>
  );
}

/** A card: a titled section with its ⓘ. */
export function Card({
  title,
  topic,
  children,
  wide = false,
}: {
  title: string;
  topic?: HelpKey;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "card wide" : "card"}>
      <div className="card-title">
        <h3>{title}</h3>
        {topic ? <Info topic={topic} /> : null}
      </div>
      {children}
    </div>
  );
}

/** Dark by default; light on the switch, remembered in this browser only. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  useEffect(() => {
    if (document.documentElement.dataset.theme === "light") setTheme("light");
  }, []);
  const flip = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // A browser without storage keeps the choice for this page only.
    }
    setTheme(next);
  };
  return (
    <button type="button" className="ghost" onClick={flip} aria-pressed={theme === "light"}>
      {theme === "dark" ? "Light theme" : "Dark theme"}
    </button>
  );
}

/** A row of choices, one pressed: a radio group by the keyboard and screen readers. */
export function Segmented<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      <span className="segmented-label">{label}</span>
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={o.value === value ? "on" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A small label: interim, counts only, paper only and the like. */
export function Chip({ children, tone }: { children: ReactNode; tone?: "warn" | "good" | "info" }) {
  return <span className={tone ? `chip ${tone}` : "chip"}>{children}</span>;
}
