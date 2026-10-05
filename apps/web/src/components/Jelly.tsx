export type JellyVariant = "plain" | "headphones" | "reader" | "pencil" | "graduate" | "glasses" | "wave";

/** Separate vector parts let the eyes, bell, tentacles and props respond to input. */
export function Jelly({ variant = "plain", happy = false }: { variant?: JellyVariant; happy?: boolean }) {
  return <svg viewBox="0 0 200 210" fill="white" stroke="#171717" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <g className="jelly-tentacles">
      {/* Reference silhouette: compact roots, tapered ink strokes and gray
          recessed strands. No outline doubles the width of the fine tips. */}
      <g stroke="none" fill="#292927">
        <path d="M53 108C54 131 48 142 39 154C30 166 33 175 43 177C37 171 41 164 48 156C60 141 62 126 62 108Z" />
        <path d="M67 108C70 130 67 149 59 163C50 178 52 187 61 190C56 183 62 175 68 166C79 149 77 128 76 108Z" fill="#a2a2a0" />
        <path d="M80 108C83 131 82 150 77 165C73 179 65 190 72 198C67 187 86 179 88 164C91 145 86 127 88 108Z" />
        <path d="M94 108C90 132 96 148 101 164C107 181 105 196 94 207C107 205 115 192 113 179C112 161 101 142 104 108Z" />
        <path d="M108 108C105 132 111 149 119 165C128 183 128 196 120 202C134 199 133 183 127 170C120 152 113 134 116 108Z" fill="#b3b3b0" />
        <path d="M121 108C118 128 125 145 133 160C142 176 141 188 132 192C146 192 148 179 141 165C132 147 126 130 129 108Z" />
        <path d="M132 108C130 129 140 145 151 158C160 169 159 179 151 181C164 182 167 172 159 160C147 144 137 130 139 108Z" fill="#a2a2a0" />
        <path d="M143 108C140 131 151 145 160 157C170 170 166 181 156 183C174 184 178 171 168 157C156 141 148 127 151 108Z" />
      </g>
    </g>
    <g className="jelly-bell">
      <path d="M18 97C19 54 53 17 94 15C139 12 172 49 183 87C196 118 164 120 104 120C55 122 12 123 18 97Z" />
      <path d="M27 95C29 60 54 33 77 28C46 52 38 94 45 104C49 111 67 113 80 115C41 116 25 111 27 95Z" fill="#d0d0ce" stroke="none" />
      <path d="M142 38C163 60 174 91 166 101C162 106 155 108 148 110" fill="none" stroke="#dededb" strokeWidth="7" />
      <g className="jelly-eyes" fill="#171717" stroke="none">
        {happy ? <><path d="M66 88Q75 72 83 88" fill="none" stroke="#171717" strokeWidth="5" /><path d="M122 88Q130 72 138 88" fill="none" stroke="#171717" strokeWidth="5" /></> : <><ellipse cx="75" cy="87" rx="6.3" ry="9.5" /><ellipse cx="132" cy="84" rx="6.3" ry="9.5" /><ellipse cx="73" cy="83" rx="1.6" ry="2" fill="white" /><ellipse cx="130" cy="80" rx="1.6" ry="2" fill="white" /></>}
      </g>
      <path d={happy ? "M94 94Q104 111 115 94Z" : "M95 96Q103 104 111 95"} fill={happy ? "#171717" : "none"} strokeWidth="3.5" />
      {happy && <g stroke="#aaa" strokeWidth="2"><path d="M58 97l-3 6m9-5-3 6m82-9-3 6m9-5-3 6" /></g>}
    </g>
    {variant === "headphones" && <g className="jelly-headphones"><path d="M17 88C-3 1 158-26 187 74" fill="none" strokeWidth="12" /><path d="M17 80C7 4 157-14 182 74" fill="none" stroke="white" strokeWidth="3" /><rect x="5" y="68" width="34" height="51" rx="16" fill="#171717" /><path d="M18 77Q8 92 19 109M179 70Q193 84 183 102" fill="none" stroke="white" strokeWidth="3" /><path d="M181 69Q197 67 196 86Q196 105 184 108" fill="#171717" /></g>}
    {(variant === "glasses" || variant === "reader") && <g className="jelly-glasses" fill="none" strokeWidth="4"><circle cx="74" cy="89" r="18" /><circle cx="133" cy="87" r="18" /><path d="M93 85q11-10 22-1M55 85l-9-4m107 2 12-6" /></g>}
    {variant === "reader" && <g className="jelly-book"><path d="M25 125Q63 114 100 140Q131 114 179 118L172 179Q129 168 102 187Q68 170 29 178Z" fill="#171717" /><path d="M100 143v41M31 125Q64 122 94 142m13-1q29-18 61-16" stroke="white" fill="none" strokeWidth="2" /><path className="jelly-book-page" d="M103 142Q133 125 164 126L164 130Q135 129 103 146Z" fill="white" stroke="none" /><ellipse cx="31" cy="154" rx="13" ry="10" /><ellipse cx="172" cy="149" rx="13" ry="10" /></g>}
    {variant === "graduate" && <g className="jelly-graduation-cap">
      {/* Mirror the lower rim about x=100; the board covers its upper seam. */}
      <path className="jelly-cap-band" d="M65 30H135V48C135 55 119 60 100 60C81 60 65 55 65 48Z" fill="#171717" />
      <path d="M26 28L93 0L181 17L112 47Z" fill="#171717" />
      <path d="M177 18v48l-7 17h15l-6-18" fill="none" />
    </g>}
    {variant === "pencil" && <g transform="rotate(-32 49 143)"><g className="jelly-pencil"><path d="M35 77h22v100l-11 27-11-27Z" /><path d="M41 81v94m10-94v94M35 91h22m-20 87h18m-13 15h8" fill="none" strokeWidth="2" /><ellipse cx="58" cy="153" rx="15" ry="10" /></g></g>}
    {variant === "wave" && <g><path d="M49 130C26 106 7 129 25 141C39 151 52 146 49 130Z" /><path className="jelly-wave-hand" d="M155 129C182 130 173 94 188 96C208 101 198 148 175 151C164 155 155 145 155 129Z" /></g>}
  </svg>;
}

