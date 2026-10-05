import { useEffect, useRef, useState } from "react";
import { Jelly, type JellyVariant } from "./Jelly";
import { t } from "../i18n/locale";

export type WorkflowSceneKind = "data" | "model" | "experiment" | "paper" | "code" | "log" | "delivery";
const labels: Record<WorkflowSceneKind, string> = {
  data: "和水母一起整理数据", model: "和水母连接模型思路", experiment: "和水母描绘实验曲线",
  paper: "和水母展开稿纸", code: "和水母推敲代码", log: "和水母翻阅运行记录", delivery: "和水母整理成果文件",
};

/** Decorative stage-specific scene: no illustrative values are presented as real results. */
export function WorkflowScene({ kind, pose }: { kind: WorkflowSceneKind; pose: JellyVariant }) {
  const ref = useRef<HTMLButtonElement>(null);
  const [active, setActive] = useState(false);
  const [reaction, setReaction] = useState(0);
  const [happy, setHappy] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    let visible = false;
    const update = () => setActive(visible && !document.hidden && !media.matches && document.documentElement.dataset.reduceMotion !== "on");
    const observer = new IntersectionObserver(entries => { visible = entries[0]?.isIntersecting ?? false; update(); });
    if (ref.current) observer.observe(ref.current);
    const preferences = new MutationObserver(update);
    preferences.observe(document.documentElement, { attributes: true, attributeFilter: ["data-reduce-motion"] });
    media.addEventListener("change", update);
    document.addEventListener("visibilitychange", update);
    return () => { observer.disconnect(); preferences.disconnect(); media.removeEventListener("change", update); document.removeEventListener("visibilitychange", update); };
  }, []);
  useEffect(() => {
    if (!happy) return;
    const timer = window.setTimeout(() => setHappy(false), 1300);
    return () => window.clearTimeout(timer);
  }, [happy, reaction]);

  return <button ref={ref} type="button" className="workflow-scene" data-kind={kind} data-moving={active ? "on" : "off"}
    aria-label={t(labels[kind])} title={t(labels[kind])} onClick={() => { setReaction(count => count + 1); setHappy(true); }}>
    <span className="workflow-scene-art" key={reaction} data-reacting={happy ? "yes" : "no"} aria-hidden="true">
      <svg className="workflow-scene-board" viewBox="0 0 320 200" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="163" cy="184" rx="112" ry="6" fill="#eff1f3" stroke="none" />
        <g className="scene-board">
          <rect x="107" y="27" width="170" height="133" rx="12" fill="#fff" stroke="#c4cad1" />
          <path d="M108 49H276" stroke="#e4e7eb" />
          <circle cx="120" cy="38" r="2" fill="#b7bfc9" stroke="none" /><circle cx="128" cy="38" r="2" fill="#d5d9de" stroke="none" />
          {kind === "data" && <g>
            <path d="M129 69H257M129 94H257M129 119H257M129 143H257M166 69V143M210 69V143" stroke="#e0e4e8" />
            {[0,1,2].map(row => <g className={`scene-data-row scene-delay-${row}`} key={row}>
              <rect x="134" y={76+row*25} width="21" height="8" rx="3" fill="#bdc5cf" stroke="none" />
              <rect x="174" y={76+row*25} width={23+row*3} height="8" rx="3" fill="#e4e8ec" stroke="none" />
              <path d={`M224 ${80+row*25}l4 4 8-9`} stroke="#637588" />
            </g>)}
            <path className="scene-scan" d="M122 70H263" stroke="#9cabbc" strokeWidth="2" />
          </g>}
          {kind === "model" && <g>
            <path className="scene-draw" pathLength="1" d="M143 106L191 74L241 106L191 139ZM143 106H241M191 74V139" stroke="#98a5b4" />
            {[[143,106],[191,74],[241,106],[191,139]].map(([x,y],i)=><g className={`scene-node scene-delay-${i%3}`} key={x+','+y}><circle cx={x} cy={y} r="10" fill="#f6f8fa" /><circle cx={x} cy={y} r="3" fill="#7a899b" stroke="none" /></g>)}
            <circle className="scene-signal" r="3" fill="#566b82" stroke="none" />
          </g>}
          {kind === "experiment" && <g>
            <path d="M132 64V142H258" stroke="#c6ccd4" /><path d="M133 88H255M133 115H255" stroke="#edf0f3" strokeDasharray="3 5" />
            <path className="scene-draw" pathLength="1" d={reaction%2 ? "M136 131C156 127 161 89 179 105S211 119 223 86S243 75 253 67" : "M136 131C155 130 155 104 173 114S205 86 219 95S240 74 253 68"} stroke="#566b82" strokeWidth="2.6" />
            <path className="scene-baseline" pathLength="1" d="M136 133Q187 126 252 111" stroke="#bdc5cf" strokeDasharray="4 5" />
            <circle className="scene-node" cx="253" cy="68" r="4" fill="#566b82" stroke="white" />
          </g>}
          {(kind === "paper" || kind === "log" || kind === "code") && <g>
            {kind === "code" && <path d="M149 67l-10 8 10 8m17-16 10 8-10 8m-9-19-4 23" stroke="#61758b" />}
            {kind === "paper" && <path d="M134 68H200" stroke="#66778b" strokeWidth="4" />}
            {[0,1,2,3].map(i => <g className={`scene-line scene-delay-${i%3}`} key={i}>
              {kind === "log" && <circle cx="137" cy={74+i*20} r="3" fill="#a6b2bf" stroke="none" />}
              <path pathLength="1" d={`M${kind==='log'?149:kind==='code'&&i%2?151:134} ${kind==='code'?98+i*13:kind==='paper'?88+i*16:74+i*20}h${[101,76,92,61][(i+reaction)%4]}`} stroke={i===1?'#a7b4c2':'#d3d9e0'} strokeWidth="3" />
            </g>)}
            {kind === "paper" && <path className="scene-draw" pathLength="1" d="M224 139l6-25 8 2-6 25-5 6Z" fill="#f6f7f9" stroke="#8795a6" />}
          </g>}
          {kind === "delivery" && <g>
            {[0,1,2].map(i=><g className={`scene-file scene-delay-${i}`} key={i}><path d={`M${139+i*24} ${71+i*8}h34l12 12v54h-46Z`} fill={['#f0f3f6','#f8f9fa','#fff'][i]} stroke="#a5b0bd" /><path d={`M${151+i*24} ${98+i*8}h21m-21 10h16`} stroke="#ccd3db" /></g>)}
          </g>}
        </g>
        <g className="scene-floating-sheet"><rect x="264" y="119" width="32" height="40" rx="5" fill="white" stroke="#bbc5d0" /><path d="M272 130h15m-15 6h10m-10 6h13" stroke="#bac5d1" /></g>
        {happy && <g className="scene-reaction" stroke="#8192a6"><path d="M76 27v9m-4-4h8m220 30v9m-4-4h8M284 85l4-5m-217 82-5 4" /></g>}
      </svg>
      <span className="workflow-scene-jelly"><Jelly variant={happy ? "wave" : pose} happy={happy} /></span>
    </span>
  </button>;
}
