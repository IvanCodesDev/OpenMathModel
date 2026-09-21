/** Visual selectors delegate to the directory's existing, authoritative filters. */
export function bindPaperFilterWheels(root: HTMLElement) {
  const refreshers: Array<() => void> = [];
  root.querySelectorAll<HTMLElement>('[data-paper-wheel]').forEach(wheel => {
    const source = wheel.querySelector<HTMLElement>('[data-wheel-options]')!;
    const viewport = wheel.querySelector<HTMLElement>('[data-wheel-viewport]')!;
    const previous = wheel.querySelector<HTMLButtonElement>('[data-wheel-step="-1"]')!;
    const next = wheel.querySelector<HTMLButtonElement>('[data-wheel-step="1"]')!;
    let options: HTMLButtonElement[] = [];
    let selected: HTMLButtonElement | undefined;
    let requestedStep: number | null = null;
    let labels = '';
    let position = 0;
    let target = 0;
    let frame = 0;
    let slotCount = 5;
    const measure = document.createElement('canvas').getContext('2d')!;
    const nodes = new Map<number, { button: HTMLButtonElement; divider: HTMLSpanElement }>();
    const modulo = (index: number, count: number) => (index % count + count) % count;
    const compact = () => options.length <= 3;
    const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
      || document.documentElement.dataset.reduceMotion === 'on';

    const band = document.createElement('div');
    band.className = 'paper-wheel-band';
    band.setAttribute('aria-hidden', 'true');
    const focus = document.createElement('div');
    focus.className = 'paper-wheel-focus';
    band.append(focus);
    const choices = document.createElement('div');
    choices.className = 'paper-wheel-choices';
    viewport.replaceChildren(band, choices);

    const fitLabel = (button: HTMLButtonElement) => {
      const label = button.getAttribute('aria-label') || '';
      const base = label.length > 12 ? 12 : 14;
      measure.font = '600 ' + base + 'px ' + getComputedStyle(viewport).fontFamily;
      const widestWord = Math.max(0, ...(label.match(/[A-Za-z0-9/-]+/g) || []).map(word => measure.measureText(word).width));
      const room = viewport.clientWidth / slotCount - 18;
      button.style.fontSize = Math.max(11, Math.min(base, widestWord ? base * room / widestWord : base)) + 'px';
    };

    const createSlot = (slot: number) => {
      const option = options[modulo(slot, options.length)];
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'paper-wheel-option';
      const label = option.textContent || '';
      const parts = label.match(/^(.+?)（(.+)）$/);
      const name = document.createElement('span');
      name.textContent = parts ? parts[1] : label;
      button.append(name);
      if (parts) {
        const detail = document.createElement('small');
        detail.textContent = '(' + parts[2] + ')';
        button.append(detail);
      }
      button.classList.toggle('long-label', label.length > 12);
      button.title = label;
      button.setAttribute('aria-label', label);
      fitLabel(button);
      button.addEventListener('click', () => {
        requestedStep = slot - target;
        option.click();
      });
      const divider = document.createElement('span');
      divider.className = 'paper-wheel-divider';
      choices.append(button);
      band.append(divider);
      const node = { button, divider };
      nodes.set(slot, node);
      return node;
    };

    const paint = () => {
      if (!options.length) return;
      const isCompact = compact();
      const half = (slotCount - 1) / 2;
      const start = isCompact ? 0 : Math.floor(position) - Math.ceil(half) - 1;
      const end = isCompact ? options.length - 1 : Math.ceil(position) + Math.ceil(half) + 1;
      for (const [slot, node] of nodes) {
        if (slot < start || slot > end) {
          node.button.remove();
          node.divider.remove();
          nodes.delete(slot);
        }
      }
      // The selected sector fills the track's full height and is clipped by
      // exactly the same upper/lower curves as its neighbours.
      focus.style.left = isCompact ? (position * 100 / slotCount) + '%' : ((100 - 100 / slotCount) / 2) + '%';
      for (let slot = start; slot <= end; slot++) {
        const { button, divider } = nodes.get(slot) || createSlot(slot);
        const offset = isCompact ? slot - half : slot - position;
        const normal = offset * 2 / slotCount;
        button.style.transform = 'translateX(' + (offset * 100) + '%) translateY(' + (22 * normal * normal) + 'px)';
        const visibility = isCompact ? 1 : Math.min(1, Math.max(0, half + 1 - Math.abs(offset)));
        button.style.opacity = String(visibility);
        button.style.pointerEvents = visibility > .2 ? '' : 'none';
        button.classList.toggle('active', slot === target);
        button.setAttribute('aria-pressed', String(slot === target));
        button.setAttribute('aria-hidden', String(visibility < .2));
        button.tabIndex = slot === target ? 0 : -1;
        divider.style.left = (50 + (offset + .5) * 100 / slotCount) + '%';
      }
    };

    const layout = () => {
      const width = viewport.clientWidth;
      slotCount = compact() ? Math.max(1, options.length) : width < 420 ? 3 : 5;
      viewport.style.setProperty('--wheel-slot', (100 / slotCount) + '%');
      viewport.style.setProperty('--wheel-start', ((100 - 100 / slotCount) / 2) + '%');
      wheel.dataset.wheelLayout = compact() ? 'compact' : 'ring';
      band.style.clipPath = "path('M 0 28 Q " + width / 2 + " -28 " + width + " 28 L " + width + " 78 Q " + width / 2 + " 46 0 78 Z')";
      nodes.forEach(node => fitLabel(node.button));
      paint();
    };
    const observer = new ResizeObserver(layout);
    observer.observe(viewport);

    const move = (destination: number, animate: boolean) => {
      cancelAnimationFrame(frame);
      target = destination;
      const from = position;
      const distance = target - from;
      if (!animate || reduced() || Math.abs(distance) < .001) {
        position = target;
        wheel.dataset.wheelMoving = 'false';
        paint();
        return;
      }
      const started = performance.now();
      const duration = Math.min(580, 330 + Math.abs(distance) * 55);
      wheel.dataset.wheelMoving = 'true';
      paint();
      const tick = (now: number) => {
        if (!root.isConnected) { observer.disconnect(); return; }
        const progress = reduced() ? 1 : Math.min(1, (now - started) / duration);
        position = from + distance * (1 - Math.pow(1 - progress, 3));
        paint();
        if (progress < 1) frame = requestAnimationFrame(tick);
        else { position = target; wheel.dataset.wheelMoving = 'false'; }
      };
      frame = requestAnimationFrame(tick);
    };

    const step = (direction: number) => {
      if (options.length < 2) return;
      const index = modulo(options.indexOf(selected!) + direction, options.length);
      requestedStep = compact() ? index - target : direction;
      options[index].click();
    };
    previous.addEventListener('click', () => step(-1));
    next.addEventListener('click', () => step(1));
    viewport.addEventListener('keydown', event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        step(event.key === 'ArrowLeft' ? -1 : 1);
        choices.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus({ preventScroll: true });
      }
    });
    refreshers.push(() => {
      const visible = [...source.querySelectorAll<HTMLButtonElement>('button')].filter(button => !button.hidden);
      const field = wheel.dataset.paperWheel;
      const key = (button: HTMLButtonElement) => field === 'competition' ? button.dataset.paperCompetitionFilter
        : field === 'group' ? button.dataset.paperGroupFilter : button.dataset.resourceTab;
      const front = field === 'competition' ? ['', 'MathorCup', '华数杯']
        : field === 'group' ? ['', 'A', 'B'] : ['全部', '一等奖', '特等奖'];
      const back = field === 'competition' ? ['美赛', '国赛']
        : field === 'group' ? ['D', 'C'] : ['优秀作品', '二等奖'];
      const available = [
        ...front.flatMap(value => visible.filter(button => key(button) === value)),
        ...visible.filter(button => !front.includes(key(button) || '') && !back.includes(key(button) || '')),
        ...back.flatMap(value => visible.filter(button => key(button) === value)),
      ];
      const active = available.find(button => button.classList.contains('active')) || available[0];
      const nextLabels = available.map(option => option.textContent).join('|');
      const scopeChanged = available.length !== options.length || available.some((option, index) => option !== options[index]) || nextLabels !== labels;
      const selectionChanged = active !== selected;
      options = available;
      selected = active;
      labels = nextLabels;
      previous.disabled = next.disabled = options.length < 2;
      if (scopeChanged) {
        cancelAnimationFrame(frame);
        nodes.forEach(node => { node.button.remove(); node.divider.remove(); });
        nodes.clear();
        position = target = Math.max(0, options.indexOf(active!));
        wheel.dataset.wheelMoving = 'false';
        layout();
      } else if (requestedStep !== null || selectionChanged) {
        // Retarget from the currently painted position, without rebuilding the
        // moving strip or snapping an in-flight animation back to a slot.
        move(requestedStep !== null ? target + requestedStep : options.indexOf(active!), requestedStep !== null);
      }
      requestedStep = null;
    });
  });
  return () => refreshers.forEach(refresh => refresh());
}
