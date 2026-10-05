import { useState, type CSSProperties } from "react";

import { Jelly, type JellyVariant } from "../components/Jelly";
type Kind = "jelly" | "books" | "book" | "board" | "graph" | "bars" | "network" | "laptop" | "plant" | "planet" | "pyramid" | "note" | "bulb" | "envelope" | "cube" | "limit" | "cloud";
type Item = { kind: Kind; x: number; y: number; w: number; r?: number; variant?: JellyVariant; text?: string; front?: boolean };


function Prop({ kind, text, active }: { kind: Kind; text?: string; active: boolean }) {
  const ink = "#171717";
  const lightBoard = text === "E = mc²" || text === "Σ" || text === "?";
  return <svg viewBox="0 0 200 180" fill="white" stroke={ink} strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === "cloud" && <g><path d="M22 126C-3 112 4 83 25 81C11 52 44 34 60 44C65 6 114 8 124 35C148 18 175 38 169 59C201 67 201 105 181 116C198 140 169 166 148 150C130 178 100 167 91 153C66 174 39 158 42 140C27 146 16 137 22 126Z" /><path d={active ? "M71 96q8-10 15 0m31 0q8-10 15 0M91 112q13 14 25-2" : "M75 96v3m49-4v3M96 111q7 6 14-1"} fill="none" strokeWidth="5" /></g>}
    {kind === "books" && <g className="prop-pages">{[0, 1, 2, 3].map(i => <g key={i} transform={`translate(${i % 2 ? 8 : 0} ${i * 36})`}><path d="M9 28L54 9L188 21L149 43Z" fill={i % 2 ? ink : "white"} /><path d="M9 29v23l138 17 42-25V22l-40 22Z" /><path d="M149 44q-7 10 0 22M14 37l125 17M15 44l123 17M159 45l24-14m-23 21 21-13" fill="none" strokeWidth="1.6" /></g>)}</g>}
    {kind === "book" && <g className="prop-pages"><path d="M7 43Q51 19 102 42Q148 17 192 38L180 141Q137 125 98 151Q52 130 8 147Z" /><path d="M15 38Q52 15 102 37Q146 11 184 32L174 130Q132 118 98 146Q48 121 2 140Z" /><path d="M102 39l-4 106" /><g fill="none" strokeWidth="2">{[0, 1, 2, 3, 4].map(i => <path key={i} d={`M22 ${51 + i * 16}q31-9 64 5m29-3q29-16 53-9`} />)}</g></g>}
    {["board", "graph", "bars", "network", "pyramid", "note", "limit"].includes(kind) && <>
      <path d="M13 10L187 18Q195 19 194 31L185 162Q185 172 174 170L10 157Q3 157 5 143L7 20Q7 10 13 10Z" />
      <path d="M17 19L183 26L176 157L14 146Z" fill={(kind === "board" && !lightBoard) || kind === "network" ? ink : "white"} strokeWidth="1.5" />
      <path d="M19 13l14 1m130 151 10 1" stroke="#aaa" strokeWidth="2" />
    </>}
    {kind === "board" && <text x="98" y={text && text.length === 1 && !active ? 118 : 100} fill={lightBoard ? ink : "white"} stroke="none" textAnchor="middle" fontFamily="Georgia, serif" fontStyle="italic" fontSize={active ? 27 : text?.length === 1 ? 80 : text && text.length > 8 ? 25 : 35}>{active ? "eⁱπ + 1 = 0" : text ?? "∫ f(x) dx"}</text>}
    {kind === "limit" && <g><path d="M50 17V6q0-15 12-13t12 26m50 0V8q0-15 12-13t12 26" fill="none" /><g fill={ink} stroke="none" fontFamily="Georgia, serif" fontStyle="italic"><text x="31" y="86" fontSize="30">lim</text><text x="28" y="108" fontSize="16">x→0</text><text x="87" y="75" fontSize="27">sin x</text><text x="106" y="111" fontSize="27">x</text><text x="142" y="98" fontSize="25">= 1</text></g><path d="M84 85h51" strokeWidth="2" />{active && <path d="M146 126l10 9 20-22" fill="none" strokeWidth="4" />}</g>}
    {kind === "graph" && <g fill="none" strokeWidth="2"><path d="M35 118l128 9m-101 17V36m-4 7 4-7 4 7m90 79 7 5-8 3" /><path d={active ? "M35 113C67 156 73 39 100 57S131 144 164 73" : "M35 121C55 122 70 26 93 52S107 154 134 116S150 76 166 82"} className="prop-curve" strokeWidth="3" /><path d="M35 70l128 10M34 95l130 9" stroke="#bbb" strokeDasharray="4 6" /></g>}
    {kind === "bars" && <g fill={ink} strokeWidth="1"><path d="M35 133h128M40 51v81" fill="none" /><rect className="bar bar-one" x="50" y={active ? 67 : 113} width="18" height={active ? 66 : 20} /><rect className="bar bar-two" x="78" y={active ? 99 : 92} width="18" height={active ? 34 : 41} /><rect className="bar bar-three" x="106" y={active ? 48 : 72} width="18" height={active ? 85 : 61} /><rect className="bar bar-four" x="134" y={active ? 78 : 43} width="18" height={active ? 55 : 90} /></g>}
    {kind === "network" && <g className="prop-orbit" fill="none" stroke="white" strokeWidth="2"><path d="M37 99l34-54 36 29 30-41 29 29-26 37-23 33-47 11-33-44 80 33-10-58-37 69 1-98 46 87 20-99m-100 66 70-25 33 25" />{[[37,99],[71,45],[107,74],[137,33],[166,62],[140,99],[117,132],[70,143]].map(([x,y],i) => <circle key={i} cx={x} cy={y} r="5" fill={active && i % 2 ? "white" : ink} />)}</g>}
    {kind === "pyramid" && <g className="prop-orbit" fill="none" strokeWidth="2"><path d="M41 128L96 36L160 135L112 153Z M96 36l16 117m-71-25 77-21 42 28" /><path d="M96 36l22 71-6 46" strokeDasharray="4 4" />{[[41,128],[96,36],[160,135],[112,153]].map(([x,y],i)=><circle key={i} cx={x} cy={y} r="2" fill={ink} />)}</g>}
    {kind === "note" && <g strokeWidth="2"><path d="M53 13V5q0-9 10-8t10 19m48 0V9q0-9 10-8t10 19" />{[0,1,2].map(i=><g key={i} transform={`translate(0 ${i*30})`}><rect x="33" y="47" width="13" height="13" /><path d={active ? "M35 53l4 4 7-8" : "M36 52l3 4 5-6"} /><path d="M58 50h93m-93 8h69" /></g>)}</g>}
    {kind === "laptop" && <g><path d="M40 16L184 6L168 125L25 136Z" /><path d="M47 24L175 17L159 117L35 127Z" fill={active ? ink : "#f0f0ee"} strokeWidth="1.5" /><ellipse cx="105" cy="72" rx="8" ry="10" fill="white" stroke="#bbb" /><path d="M25 135L168 124L194 146L62 173L3 151Z" /><path d="M7 151l56 15 130-23M37 142l99-10 24 10-94 17Z" fill="none" strokeWidth="1.5" /></g>}
    {kind === "plant" && <g><path d="M100 141V34M98 120L56 66m45 30 42-46" fill="none" /><path d="M98 66Q62 37 98 3Q121 28 98 66ZM94 98Q36 98 32 47Q72 52 94 98ZM103 88Q109 39 151 34Q152 79 103 88ZM101 126Q125 80 171 94Q154 130 101 126ZM93 133Q41 135 29 103Q68 91 93 133Z" fill={ink} /><path d="M64 139Q99 150 137 139L130 177H70Z" /><ellipse cx="100" cy="140" rx="37" ry="8" /><path d="M73 151l4 18" stroke="#aaa" /></g>}
    {kind === "planet" && <g className="prop-orbit"><circle cx="103" cy="86" r="55" fill={ink} /><path d="M51 72C-25 124 24 154 130 109S211 35 153 51" fill="none" strokeWidth="9" /><path d="M51 72C-25 124 24 154 130 109S211 35 153 51" fill="none" stroke="white" strokeWidth="4" /><path d="M88 43q20-6 35 4m17 10 4 5" fill="none" stroke="white" /></g>}
    {kind === "bulb" && <g><path d="M63 64C54 7 139 9 139 59C139 83 119 88 120 111H81C83 90 63 88 63 64Z" fill={active ? "#dededb" : "white"} /><path d="M82 114h37m-35 8h33m-29 8h24m-19 8h14M94 108L85 64l14 11 14-12-7 45M98 6V0m-48 31-16-9m109 2 16-13M43 64H26m128-6h20" fill="none" /></g>}
    {kind === "envelope" && <g><path d="M20 46L171 22L180 132L27 157Z" /><path d="M20 46l83 57 68-81M27 157l56-70m33 5 64 40" fill="none" /></g>}
    {kind === "cube" && <g className="prop-orbit"><circle cx="100" cy="90" r="77" fill={ink} /><g stroke="white" fill="none" strokeWidth="2"><path d="M48 39L117 27L155 60L85 78ZM48 39l-5 84 42 33 70-18V60M85 78v78" /><path d="M43 123l68-19 44 34m-44-34 6-77" stroke="#aaa" /></g></g>}
  </svg>;
}

// Positions measured on the supplied 1848 × 1018 composition. No raster crops.
const items: Item[] = [
  {kind:"books",x:-30,y:138,w:185,r:-8},{kind:"books",x:313,y:-38,w:144,r:3},{kind:"board",x:254,y:40,w:210,r:-14},
  {kind:"board",x:757,y:-35,w:220,r:13,text:"Σ 1/i² = π²/6"},{kind:"book",x:635,y:113,w:188,r:9},
  {kind:"envelope",x:692,y:0,w:102,r:-5},{kind:"plant",x:1279,y:45,w:100},{kind:"bars",x:1352,y:-39,w:150,r:-12},
  {kind:"books",x:1505,y:145,w:137,r:8},{kind:"books",x:1777,y:82,w:155,r:-5},{kind:"board",x:1690,y:175,w:211,r:-9,text:"a²+b²=c²"},
  {kind:"books",x:7,y:474,w:172,r:8},{kind:"graph",x:197,y:307,w:243,r:8},{kind:"cube",x:356,y:130,w:121,r:7},
  {kind:"laptop",x:183,y:126,w:153,r:-7},{kind:"book",x:-39,y:266,w:139,r:11},{kind:"note",x:578,y:412,w:155,r:-8},
  {kind:"board",x:1241,y:321,w:211,r:-11,text:""},{kind:"network",x:1250,y:329,w:204,r:-11},
  {kind:"board",x:1345,y:204,w:123,r:1,text:"</>"},{kind:"books",x:1174,y:452,w:195,r:-4},
  {kind:"books",x:1535,y:455,w:162,r:9},{kind:"note",x:1484,y:621,w:177,r:9},{kind:"planet",x:1672,y:510,w:168,r:-10},
  {kind:"plant",x:1774,y:338,w:132,r:8},{kind:"plant",x:-16,y:337,w:115,r:-8},{kind:"plant",x:547,y:535,w:112,r:12},
  {kind:"board",x:3,y:460,w:143,r:-10,text:"Σ"},{kind:"board",x:140,y:474,w:110,r:-14,text:"?"},
  {kind:"limit",x:368,y:464,w:205,r:10},{kind:"pyramid",x:302,y:588,w:178,r:10},
  {kind:"bars",x:1302,y:550,w:109,r:-5},{kind:"cube",x:1424,y:617,w:93,r:3},
  {kind:"laptop",x:136,y:637,w:141,r:-5},{kind:"graph",x:182,y:759,w:160,r:-12},{kind:"note",x:343,y:741,w:154,r:-12},
  {kind:"books",x:330,y:839,w:233,r:4},{kind:"books",x:644,y:848,w:250,r:-3},{kind:"book",x:879,y:895,w:135,r:11},
  {kind:"books",x:1077,y:835,w:211,r:3},{kind:"books",x:1117,y:957,w:175,r:-9},{kind:"board",x:1221,y:891,w:188,r:-8,text:"E = mc²"},
  {kind:"bars",x:1632,y:783,w:215,r:-12},{kind:"note",x:1681,y:936,w:100,r:-10},{kind:"book",x:107,y:944,w:152,r:13},
  {kind:"plant",x:869,y:973,w:170},{kind:"plant",x:1769,y:657,w:136,r:-5},{kind:"book",x:1351,y:812,w:148,r:-8},
  {kind:"jelly",x:32,y:-15,w:74,r:8},{kind:"jelly",x:238,y:-11,w:96,r:9},{kind:"jelly",x:435,y:-76,w:135,r:-7},
  {kind:"jelly",x:57,y:40,w:189,r:-4,variant:"headphones"},{kind:"jelly",x:492,y:2,w:176,r:-12,variant:"pencil"},
  {kind:"jelly",x:825,y:38,w:148,r:7},{kind:"jelly",x:966,y:45,w:83,r:5},
  {kind:"jelly",x:1093,y:-30,w:173,r:5,variant:"reader"},{kind:"jelly",x:1477,y:-12,w:82,r:3},
  {kind:"jelly",x:1613,y:12,w:174,r:-8,variant:"graduate"},{kind:"jelly",x:1772,y:-8,w:84,r:6},
  {kind:"jelly",x:1443,y:90,w:130,r:-10},{kind:"jelly",x:1225,y:177,w:145,r:8},
  {kind:"jelly",x:456,y:133,w:124,r:3},{kind:"jelly",x:214,y:230,w:95,r:-15},
  {kind:"jelly",x:81,y:235,w:128,r:-3},{kind:"jelly",x:501,y:272,w:155,r:-7,variant:"pencil"},
  {kind:"jelly",x:402,y:269,w:91,r:-9},{kind:"jelly",x:1580,y:329,w:182,r:3,variant:"reader"},
  {kind:"jelly",x:1709,y:278,w:116,r:9},
  {kind:"jelly",x:1460,y:370,w:132,r:5,variant:"pencil"},{kind:"jelly",x:1478,y:305,w:76,r:5},
  {kind:"jelly",x:116,y:367,w:112,r:5},{kind:"jelly",x:229,y:450,w:135,r:-4},
  {kind:"jelly",x:414,y:588,w:145,r:5},{kind:"jelly",x:1474,y:494,w:144,r:11},
  {kind:"jelly",x:1171,y:536,w:114,r:-7},{kind:"jelly",x:46,y:577,w:141,r:-5},
  {kind:"jelly",x:1583,y:670,w:166,r:-14},{kind:"jelly",x:3,y:772,w:172,r:-4,variant:"wave"},
  {kind:"jelly",x:196,y:829,w:120,r:12},{kind:"jelly",x:989,y:880,w:109,r:6},
  {kind:"jelly",x:1161,y:746,w:93,r:5},{kind:"jelly",x:1393,y:699,w:110,r:5},
  {kind:"jelly",x:1262,y:709,w:193,r:-10,variant:"pencil"},{kind:"jelly",x:1772,y:923,w:105,r:10},
  {kind:"jelly",x:467,y:753,w:265,r:-15,variant:"wave",front:true},{kind:"jelly",x:1446,y:815,w:240,r:12,variant:"wave",front:true},
];

const fillers: Item[] = [
  ...[75,230,385,540,695,850].flatMap((y,row)=>[-35,175,385,595,1155,1365,1575,1785].map((x,col):Item=>({kind:"cloud",x:x+(row%2?30:0),y,w:142+(col%2)*14,r:(row+col)%2?17:-9}))),
  ...[[327,231],[58,448],[502,462],[287,371],[6,850],[529,681],[1537,582],[1756,805],[1293,641],[1081,136],[1659,106],[1190,684],[369,936],[1421,188],[448,768],[647,755]].map(([x,y],i):Item=>({kind:(["book","books","note","plant"] as Kind[])[i%4],x,y,w:92+i%3*14,r:i%2?13:-12})),
  ...[[334,298],[266,591],[64,721],[535,870],[1545,757],[1788,456],[1289,129],[1039,41],[184,16],[1595,186],[1554,9]].map(([x,y],i):Item=>({kind:"jelly",x,y,w:86+i%3*13,r:i%2?10:-12})),
  ...[[4,10],[146,-40],[3,302],[589,189],[578,243],[601,564],[259,706],[14,916],[1777,700],[1561,469],[1182,301],[1677,265],[958,842],[1090,800],[1500,786],[1835,427],[1794,587]].map(([x,y],i):Item=>({kind:"jelly",x,y,w:60+i%3*15,r:i%2?12:-9})),
  ...[[111,10],[442,91],[661,28],[985,-21],[1252,4],[1600,126],[186,653],[1077,742],[1319,453],[1749,634]].map(([x,y],i):Item=>({kind:i%2?"envelope":"bulb",x,y,w:55+i%3*12,r:i%2?-15:12})),
  ...[[275,146],[14,686],[275,925],[1776,266],[1540,-68],[1571,548],[5,569],[1185,123],[1420,928]].map(([x,y],i):Item=>({kind:i%2?"board":"books",x,y,w:97+i%3*13,r:i%2?9:-9,text:"f(x)"})),
];

const labels: Record<Kind, string> = {jelly:"小水母",books:"书堆",book:"翻开数学书",board:"切换数学公式",graph:"变换函数曲线",bars:"更新统计图",network:"点亮数学网络",laptop:"打开小电脑",plant:"摇摇盆栽",planet:"旋转星球",pyramid:"旋转几何模型",note:"勾选学习清单",bulb:"点亮灵感",envelope:"轻触信封",cube:"旋转立方体",limit:"验证经典极限",cloud:"云朵伙伴"};

function Doodle({ item, index }: { item: Item; index: number }) {
  const [active, setActive] = useState(false);
  const [pulse, setPulse] = useState(0);
  const style = { left:`${item.x/18.48}%`,top:`${item.y/10.18}%`,width:`${item.w/18.48}%`,"--turn":`${item.r ?? 0}deg`,"--delay":`${index%7 * -.7}s` } as CSSProperties;
  return <button type="button" className={`login-doodle doodle-${item.kind}${item.front ? " doodle-front" : ""}${active ? " is-awake" : ""}`} style={style} onClick={() => {setActive(value=>!value);setPulse(value=>value+1);}} aria-label={`${labels[item.kind]} ${index + 1}`} aria-pressed={active}>
    <span className="doodle-drawing" key={pulse}>{item.kind === "jelly" ? <Jelly variant={item.variant} happy={active} /> : <Prop kind={item.kind} text={item.text} active={active} />}</span>
    {active && <span className="doodle-reaction" aria-hidden="true">{item.kind === "jelly" ? "♡" : "✦"}</span>}
  </button>;
}

export function DoodleGarden() {
  return <><div className="login-garden" role="group" aria-label="可互动的数学世界，点击水母和图案试试看">
    <svg className="garden-marks" viewBox="0 0 1848 1018" preserveAspectRatio="none" aria-hidden="true"><rect width="1848" height="1018" fill="#fcfcfa" />{Array.from({length:180},(_,i)=>{const x=(i*197+17)%1848,y=(i*131+31)%1018;return <g key={i} transform={`translate(${x} ${y}) rotate(${i*13})`} fill={i%3?"#171717":"white"} stroke="#171717" strokeWidth="2"><path d={i%4 ? "M0-8Q1-1 7 0Q1 2 0 9Q-2 2-8 0Q-2-2 0-8Z" : "M0-14L4-5 14-3 7 4 9 13 0 8-8 13-7 3-14-3-4-5Z"} /></g>;})}</svg>
    {fillers.map((item,index)=><Doodle key={`f${index}`} item={item} index={index+items.length} />)}
    {items.map((item,index)=> !item.front && <Doodle key={index} item={item} index={index} />)}
  </div><div className="login-foreground" role="group" aria-label="水母伙伴">{items.map((item,index)=>item.front && <Doodle key={index} item={item} index={index} />)}</div></>;
}
