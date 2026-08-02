import { useLayoutEffect } from "react";
import parse from "html-react-parser";
import { activateScreen, getScreenMarkup } from "../legacy/openmathmodel-ui.js";

export default function OpenMathModelScreen({ screen, title }) {
  const markup = getScreenMarkup(screen);

  useLayoutEffect(() => {
    document.title = `OpenMathModel · ${title}`;
    activateScreen(screen);
  }, [screen, title]);

  if (screen === "editor") {
    return <div className="react-html-root" dangerouslySetInnerHTML={{ __html: markup }} />;
  }

  return parse(markup);
}
