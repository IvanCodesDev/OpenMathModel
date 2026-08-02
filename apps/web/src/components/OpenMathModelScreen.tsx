import type { ReactNode } from "react";
import { useLayoutEffect } from "react";
import parse from "html-react-parser";
import { activateScreen, getScreenMarkup } from "../legacy/openmathmodel-ui";
import type { ScreenId } from "../types/screens";

interface OpenMathModelScreenProps {
  screen: ScreenId;
  title: string;
}

export default function OpenMathModelScreen({ screen, title }: OpenMathModelScreenProps): ReactNode {
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
