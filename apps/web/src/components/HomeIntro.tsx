import { useEffect, useState, useSyncExternalStore, type CSSProperties } from "react";
import { currentLocale, onLocaleChange, t } from "../i18n/locale";
import "./home-intro.css";

const characters = (text: string) => Array.from(
  new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text),
  part => part.segment,
);

function TypedLine({ text, start, interval }: { text: string; start: number; interval: number }) {
  const letters = characters(text);
  const [visibleCount, setVisibleCount] = useState(0);

  useEffect(() => {
    let count = 0;
    let timer: number;
    const revealNext = () => {
      count += 1;
      setVisibleCount(count);
      if (count < letters.length) timer = window.setTimeout(revealNext, interval);
    };
    timer = window.setTimeout(revealNext, start);
    return () => window.clearTimeout(timer);
  }, [letters.length, start, interval]);

  return <>
    <span className="home-intro-readable">{text}</span>
    <span aria-hidden="true" key={text}>
      {letters.map((character, index) => <span
        key={index}
        className="home-intro-character"
        style={{ "--character-opacity": index < visibleCount ? 1 : 0 } as CSSProperties}
      >{character}</span>)}
    </span>
  </>;
}

/** Inline characters reserve the final line breaks, avoiding layout shifts. */
export default function HomeIntro({ title, subtitle }: { title: string; subtitle: string }) {
  useSyncExternalStore(onLocaleChange, currentLocale);
  const heading = t(title);
  const lead = t(subtitle);
  // Cap each line's duration so a longer translation doesn't prolong the intro.
  const titleInterval = Math.min(48, 1100 / Math.max(characters(heading).length, 1));
  const subtitleInterval = Math.min(30, 1400 / Math.max(characters(lead).length, 1));
  const subtitleStart = 300 + characters(heading).length * titleInterval;

  return <>
    <h1 className="home-intro-line"><TypedLine key={heading} text={heading} start={120} interval={titleInterval} /></h1>
    <p className="lead home-intro-line"><TypedLine key={lead} text={lead} start={subtitleStart} interval={subtitleInterval} /></p>
  </>;
}
