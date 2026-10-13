const pptxgen = require('pptxgenjs');
const IMG_SIZES = {
  '01-system-overview.png': { width: 2880, height: 1400 },
  '02-log-collector-pipelines.png': { width: 2880, height: 1360 },
  '03-centralized-logging-role-order.png': { width: 2880, height: 1640 },
};
function sizeOf(path) {
  const name = path.split('/').pop();
  return IMG_SIZES[name];
}

const IMG_DIR = __dirname; // diagram PNGs and the built PPTX live alongside this script

// Nexus palette (no # prefix, 6-char hex only)
const BG_DARK = '171614';
const BG_LIGHT = 'F7F6F2';
const SURFACE = 'F9F8F5';
const BORDER = 'D4D1CA';
const TEXT = '28251D';
const TEXT_MUTED = '7A7974';
const TEXT_ON_DARK = 'F7F6F2';
const TEXT_ON_DARK_MUTED = 'B9B6AE';
const ACCENT = '01696F';
const ACCENT_LIGHT = 'EFF7F7';

const deck = new pptxgen();
deck.layout = 'LAYOUT_WIDE'; // 13.33 x 7.5
const PAGE_W = 13.33;
const PAGE_H = 7.5;
const MARGIN = 0.6;

function footer(sl, label, pageNum) {
  sl.addText(label, {
    x: MARGIN, y: PAGE_H - 0.42, w: 8, h: 0.3,
    fontFace: 'Calibri', fontSize: 9, color: TEXT_MUTED, align: 'left', margin: 0,
  });
  sl.addText(String(pageNum), {
    x: PAGE_W - MARGIN - 1, y: PAGE_H - 0.42, w: 1, h: 0.3,
    fontFace: 'Calibri', fontSize: 9, color: TEXT_MUTED, align: 'right', margin: 0,
  });
}

// ---------- Slide 1: Title ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_DARK };
  sl.addShape(deck.shapes.RECTANGLE, { x: 0, y: 0, w: PAGE_W, h: 0.09, fill: { color: ACCENT }, line: { type: 'none' } });

  sl.addText('Linux Fleet Automation', {
    x: MARGIN, y: 2.9, w: PAGE_W - 2 * MARGIN, h: 0.9,
    fontFace: 'Trebuchet MS', fontSize: 40, bold: true, color: TEXT_ON_DARK, margin: 0,
  });
  sl.addText('System Architecture & Operational Visibility', {
    x: MARGIN, y: 3.75, w: PAGE_W - 2 * MARGIN, h: 0.55,
    fontFace: 'Calibri', fontSize: 20, color: TEXT_ON_DARK_MUTED, margin: 0,
  });
  sl.addText('September 2026', {
    x: MARGIN, y: 6.5, w: 6, h: 0.35,
    fontFace: 'Calibri', fontSize: 12, color: TEXT_ON_DARK_MUTED, margin: 0,
  });
}

// ---------- Slide 2: Why this matters ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_LIGHT };
  sl.addText('Why this matters', {
    x: MARGIN, y: 0.5, w: PAGE_W - 2 * MARGIN, h: 0.7,
    fontFace: 'Trebuchet MS', fontSize: 28, bold: true, color: TEXT, margin: 0,
  });
  sl.addText('A single automation platform gives the fleet one consistent way to collect logs, watch for problems, and report on host health.', {
    x: MARGIN, y: 1.2, w: PAGE_W - 2 * MARGIN, h: 0.55,
    fontFace: 'Calibri', fontSize: 14, color: TEXT_MUTED, margin: 0,
  });

  const cards = [
    { title: 'Centralized visibility', body: 'Every host in the fleet forwards logs and inventory to one place \u2014 no more logging into individual servers to find out what happened.' },
    { title: 'Faster detection', body: 'Automated watchers check for service failures, disk pressure, and identity/sync issues, and raise email alerts before they become incidents.' },
    { title: 'Repeatable & safe', body: 'Every capability is built as a tested, idempotent automation role \u2014 changes are validated in a controlled environment before reaching production.' },
    { title: 'Less manual toil', body: 'Host inventory, patch history, and configuration reporting are generated automatically instead of collected by hand across the fleet.' },
  ];
  const cardW = (PAGE_W - 2 * MARGIN - 3 * 0.35) / 4;
  const cardY = 2.15, cardH = 4.5;
  cards.forEach((c, i) => {
    const x = MARGIN + i * (cardW + 0.35);
    sl.addShape(deck.shapes.RECTANGLE, {
      x, y: cardY, w: cardW, h: cardH,
      fill: { color: SURFACE }, line: { color: BORDER, width: 0.75 },
    });
    sl.addText(c.title, {
      x: x + 0.25, y: cardY + 0.3, w: cardW - 0.5, h: 0.7,
      fontFace: 'Trebuchet MS', fontSize: 15, bold: true, color: ACCENT, margin: 0, valign: 'top',
    });
    sl.addText(c.body, {
      x: x + 0.25, y: cardY + 1.05, w: cardW - 0.5, h: cardH - 1.35,
      fontFace: 'Calibri', fontSize: 12, color: TEXT, margin: 0, valign: 'top', lineSpacingMultiple: 1.25,
    });
  });
  footer(sl, 'Linux Fleet Automation \u2014 System Architecture & Operational Visibility', 2);
}

// ---------- Slide 3: System overview diagram ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_LIGHT };
  sl.addText('How it fits together', {
    x: MARGIN, y: 0.45, w: PAGE_W - 2 * MARGIN, h: 0.65,
    fontFace: 'Trebuchet MS', fontSize: 26, bold: true, color: TEXT, margin: 0,
  });
  sl.addText('Fleet hosts, the identity realm, the collector, dashboards, and email alerting all connect through one automation platform.', {
    x: MARGIN, y: 1.1, w: PAGE_W - 2 * MARGIN, h: 0.5,
    fontFace: 'Calibri', fontSize: 13, color: TEXT_MUTED, margin: 0,
  });

  const dim = sizeOf(`${IMG_DIR}/01-system-overview.png`);
  const maxW = PAGE_W - 2 * MARGIN;
  const maxH = 4.9;
  let w = maxW, h = w / (dim.width / dim.height);
  if (h > maxH) { h = maxH; w = h * (dim.width / dim.height); }
  const x = (PAGE_W - w) / 2;
  sl.addImage({ path: `${IMG_DIR}/01-system-overview.png`, x, y: 1.75, w, h });
  footer(sl, 'Linux Fleet Automation \u2014 System Architecture & Operational Visibility', 3);
}

// ---------- Slide 4: Two pipelines diagram ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_LIGHT };
  sl.addText('Two pipelines, one host', {
    x: MARGIN, y: 0.45, w: PAGE_W - 2 * MARGIN, h: 0.65,
    fontFace: 'Trebuchet MS', fontSize: 26, bold: true, color: TEXT, margin: 0,
  });
  sl.addText('Centralized logging and the fleet dashboard run independently on the same collector host \u2014 each can be upgraded or restarted without affecting the other.', {
    x: MARGIN, y: 1.1, w: PAGE_W - 2 * MARGIN, h: 0.5,
    fontFace: 'Calibri', fontSize: 13, color: TEXT_MUTED, margin: 0,
  });

  const dim = sizeOf(`${IMG_DIR}/02-log-collector-pipelines.png`);
  const maxW = PAGE_W - 2 * MARGIN;
  const maxH = 4.9;
  let w = maxW, h = w / (dim.width / dim.height);
  if (h > maxH) { h = maxH; w = h * (dim.width / dim.height); }
  const x = (PAGE_W - w) / 2;
  sl.addImage({ path: `${IMG_DIR}/02-log-collector-pipelines.png`, x, y: 1.75, w, h });
  footer(sl, 'Linux Fleet Automation \u2014 System Architecture & Operational Visibility', 4);
}

// ---------- Slide 5: Role execution order diagram ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_LIGHT };
  sl.addText('Role execution order', {
    x: MARGIN, y: 0.45, w: PAGE_W - 2 * MARGIN, h: 0.65,
    fontFace: 'Trebuchet MS', fontSize: 26, bold: true, color: TEXT, margin: 0,
  });
  sl.addText('The centralized logging automation applies roles in a fixed, dependency-aware order across three host groups: the collector, log-forwarding clients, and the identity realm.', {
    x: MARGIN, y: 1.1, w: PAGE_W - 2 * MARGIN, h: 0.5,
    fontFace: 'Calibri', fontSize: 13, color: TEXT_MUTED, margin: 0,
  });

  const dim3 = sizeOf(`${IMG_DIR}/03-centralized-logging-role-order.png`);
  const maxW3 = PAGE_W - 2 * MARGIN;
  const maxH3 = 4.9;
  let w3 = maxW3, h3 = w3 / (dim3.width / dim3.height);
  if (h3 > maxH3) { h3 = maxH3; w3 = h3 * (dim3.width / dim3.height); }
  const x3 = (PAGE_W - w3) / 2;
  sl.addImage({ path: `${IMG_DIR}/03-centralized-logging-role-order.png`, x: x3, y: 1.75, w: w3, h: h3 });
  footer(sl, 'Linux Fleet Automation \u2014 System Architecture & Operational Visibility', 5);
}

// ---------- Slide 6: Status & roadmap ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_LIGHT };
  sl.addText('Status & roadmap', {
    x: MARGIN, y: 0.5, w: PAGE_W - 2 * MARGIN, h: 0.7,
    fontFace: 'Trebuchet MS', fontSize: 28, bold: true, color: TEXT, margin: 0,
  });

  const colW = (PAGE_W - 2 * MARGIN - 0.5) / 2;
  const colY = 1.5, colH = 5.1;

  // Today
  sl.addShape(deck.shapes.RECTANGLE, { x: MARGIN, y: colY, w: colW, h: colH, fill: { color: SURFACE }, line: { color: BORDER, width: 0.75 } });
  sl.addText('Where we are today', {
    x: MARGIN + 0.3, y: colY + 0.3, w: colW - 0.6, h: 0.5,
    fontFace: 'Trebuchet MS', fontSize: 16, bold: true, color: TEXT, margin: 0,
  });
  const todayItems = [
    'Centralized logging and the fleet dashboard are both running in a controlled test environment.',
    'Access is currently protected by a single shared credential.',
    'Alert conditions are validated: disk, service-failure, and identity/sync watchers are in place.',
    'Storage growth and retention are being monitored ahead of a production sizing decision.',
  ];
  sl.addText(
    todayItems.map((t, i) => ({ text: t, options: { bullet: { code: '2022' }, breakLine: i < todayItems.length - 1, paraSpaceAfter: 10 } })),
    { x: MARGIN + 0.3, y: colY + 0.9, w: colW - 0.6, h: colH - 1.2, fontFace: 'Calibri', fontSize: 13, color: TEXT, margin: 0, valign: 'top', lineSpacingMultiple: 1.2 }
  );

  // Next
  const x2 = MARGIN + colW + 0.5;
  sl.addShape(deck.shapes.RECTANGLE, { x: x2, y: colY, w: colW, h: colH, fill: { color: ACCENT_LIGHT }, line: { color: BORDER, width: 0.75 } });
  sl.addText('What\u2019s next', {
    x: x2 + 0.3, y: colY + 0.3, w: colW - 0.6, h: 0.5,
    fontFace: 'Trebuchet MS', fontSize: 16, bold: true, color: ACCENT, margin: 0,
  });
  const nextItems = [
    'Move both dashboards to identity-realm single sign-on, replacing the shared credential.',
    'Restore TLS on both dashboards using corporate-issued certificates.',
    'Add per-request username logging so dashboard access is individually auditable.',
    'Promote validated changes into the production estate once sign-off and sizing are confirmed.',
  ];
  sl.addText(
    nextItems.map((t, i) => ({ text: t, options: { bullet: { code: '2022' }, breakLine: i < nextItems.length - 1, paraSpaceAfter: 10 } })),
    { x: x2 + 0.3, y: colY + 0.9, w: colW - 0.6, h: colH - 1.2, fontFace: 'Calibri', fontSize: 13, color: TEXT, margin: 0, valign: 'top', lineSpacingMultiple: 1.2 }
  );

  footer(sl, 'Linux Fleet Automation \u2014 System Architecture & Operational Visibility', 6);
}

// ---------- Slide 7: Closing ----------
{
  const sl = deck.addSlide();
  sl.background = { color: BG_DARK };
  sl.addShape(deck.shapes.RECTANGLE, { x: 0, y: 0, w: PAGE_W, h: 0.09, fill: { color: ACCENT }, line: { type: 'none' } });
  sl.addText('Questions & discussion', {
    x: MARGIN, y: 3.2, w: PAGE_W - 2 * MARGIN, h: 0.9,
    fontFace: 'Trebuchet MS', fontSize: 34, bold: true, color: TEXT_ON_DARK, margin: 0,
  });
  sl.addText('Happy to walk through any part of the architecture, current status, or rollout plan in more detail.', {
    x: MARGIN, y: 4.0, w: PAGE_W - 2 * MARGIN, h: 0.6,
    fontFace: 'Calibri', fontSize: 16, color: TEXT_ON_DARK_MUTED, margin: 0,
  });
}

deck.writeFile({ fileName: `${IMG_DIR}/Linux-Fleet-Automation-Architecture-Overview.pptx` }).then(() => {
  console.log('Built PPTX');
});
