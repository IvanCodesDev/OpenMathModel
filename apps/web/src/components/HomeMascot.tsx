import { useEffect, useRef, useState } from "react";
import { Jelly, type JellyVariant } from "./Jelly";
import "./home-mascot.css";

const poses: Array<{ variant: JellyVariant; label: string; duration: number }> = [
  { variant: "plain", label: "轻轻漂浮", duration: 5600 },
  { variant: "wave", label: "向你打招呼", duration: 4200 },
  { variant: "pencil", label: "拿起铅笔思考", duration: 6200 },
  { variant: "reader", label: "翻阅数学书", duration: 7000 },
  { variant: "headphones", label: "戴着耳机轻轻摇摆", duration: 5200 },
  { variant: "graduate", label: "戴上学士帽庆祝", duration: 4200 },
  { variant: "glasses", label: "戴上眼镜认真观察", duration: 5000 },
];

const mathNotes: Record<JellyVariant, [string, string]> = {
  plain: ["∑", "f(x)"],
  wave: ["π", "∞"],
  pencil: ["∫", "y = x²"],
  reader: ["∑", "a² + b²"],
  headphones: ["π", "sin x"],
  graduate: ["√", "E = mc²"],
  glasses: ["∂", "∇f = 0"],
};

// One distinct solid per pose, including the last-to-first transition.
const solids = [
  { name: "cube", label: "正方体", outline: "M252 108L269 117V136L252 145L235 136V117ZM235 117L252 126L269 117M252 126V145", hidden: "" },
  { name: "tetrahedron", label: "四面体", outline: "M252 107L232 136L252 145L272 136ZM252 107V145", hidden: "M232 136H272" },
  { name: "octahedron", label: "八面体", outline: "M252 106L232 126L252 146L272 126ZM232 126L252 132L272 126M252 106V146", hidden: "M232 126L252 120L272 126" },
  { name: "cylinder", label: "圆柱", outline: "M235 115A17 7 0 1 0 269 115A17 7 0 1 0 235 115M235 115V138A17 7 0 0 0 269 138V115", hidden: "M235 138A17 7 0 0 1 269 138" },
  { name: "cone", label: "圆锥", outline: "M252 107L233 137A19 8 0 0 0 271 137Z", hidden: "M233 137A19 8 0 0 1 271 137M252 107V137" },
  { name: "prism", label: "三棱柱", outline: "M230 135L242 110L256 138ZM242 110L258 106L272 134L256 138", hidden: "M230 135L242 131L272 134M242 131L258 106" },
  { name: "sphere", label: "球体", outline: "M271 126A19 19 0 1 0 233 126A19 19 0 1 0 271 126M252 107C240 107 240 145 252 145C264 145 264 107 252 107M233 126C233 136 271 136 271 126", hidden: "M233 126C233 116 271 116 271 126" },
];

/** Replaces only the home hero image; retains its existing layout/hide slot. */
export default function HomeMascot() {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [poseIndex, setPoseIndex] = useState(0);
  const [onScreen, setOnScreen] = useState(false);
  const [pageVisible, setPageVisible] = useState(!document.hidden);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [announcement, setAnnouncement] = useState("");
  const [spark, setSpark] = useState(0);
  const pose = poses[poseIndex];
  const solid = solids[poseIndex];
  const animate = onScreen && pageVisible && !reducedMotion;

  useEffect(() => {
    const button = buttonRef.current;
    if (!button) return;
    const observer = new IntersectionObserver(entries => setOnScreen(entries[0]?.isIntersecting ?? false));
    observer.observe(button);
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updateMotion = () => setReducedMotion(media.matches);
    const updateVisibility = () => setPageVisible(!document.hidden);
    media.addEventListener("change", updateMotion);
    document.addEventListener("visibilitychange", updateVisibility);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", updateMotion);
      document.removeEventListener("visibilitychange", updateVisibility);
    };
  }, []);

  useEffect(() => {
    if (!animate) return;
    const timer = window.setTimeout(() => setPoseIndex(index => (index + 1) % poses.length), pose.duration);
    return () => window.clearTimeout(timer);
  }, [animate, poseIndex, pose.duration]);

  function changePose() {
    const next = (poseIndex + 1) % poses.length;
    setPoseIndex(next);
    setSpark(count => count + 1);
    setAnnouncement(`小水母正在${poses[next].label}，旁边是${solids[next].label}`);
  }

  return <button
    ref={buttonRef}
    className="project-logo hero-logo home-mascot"
    type="button"
    data-pose={pose.variant}
    data-animate={animate ? "on" : "off"}
    aria-label="切换水母动作"
    title={`小水母正在${pose.label}，旁边是${solid.label}；点击切换动作和几何图形`}
    onClick={changePose}
  >
    <span className="home-mascot-math" aria-hidden="true">
      <svg viewBox="0 0 300 180" fill="none">
        <g className="home-math-note home-math-note-left">
          <text x="46" y="46" className="home-math-symbol">{mathNotes[pose.variant][0]}</text>
          <path d="M33 57q14 4 27-1" />
        </g>
        <g className="home-math-note home-math-note-right">
          <text x="249" y="64" className="home-math-formula">{mathNotes[pose.variant][1]}</text>
          <path d="M232 75q15-3 31 0" />
        </g>
        <g className="home-math-note home-math-graph">
          <path className="home-math-axis" d="M27 137h48m-39 9v-49m-3 4 3-4 3 4m32 33 4 3-4 3" />
          <path className="home-math-curve" d="M30 127C42 102 46 105 51 125S66 146 73 113" />
        </g>
        <g className="home-math-note home-math-solid" data-solid={solid.name}>
          <path d={solid.outline} />
          {solid.hidden && <path d={solid.hidden} strokeDasharray="2 3" opacity=".45" />}
        </g>
        {spark > 0 && <g key={spark} className="home-math-sparks">
          <text x="91" y="33">+</text><text x="207" y="24">π</text>
          <text x="85" y="146">×</text><text x="214" y="149">∞</text>
        </g>}
      </svg>
    </span>
    <span className="home-mascot-float" aria-hidden="true">
      <span className="home-mascot-hover">
        <span key={pose.variant} className="home-mascot-pose">
          <Jelly variant={pose.variant} happy={pose.variant === "wave" || pose.variant === "graduate"} />
        </span>
      </span>
    </span>
    <span className="home-mascot-announcement" role="status" aria-live="polite">{announcement}</span>
  </button>;
}
