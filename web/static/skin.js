// Draws the chosen Winamp skin and wires up the page's small interactions.
//
// A CSS background can't tile one sprite out of a sprite sheet, so each
// sprite is cropped onto a canvas and stored in a CSS variable
// (--sprite-<name>) that app.css uses. The browser decodes the skin's BMPs.
"use strict";

// Where each sprite sits in its sheet: [x, y, width, height] (Winamp 2 layout).
const SPRITES = {
  "pledit.bmp": {
    "pl-top-left": [0, 0, 25, 20],
    "pl-top-tile": [127, 0, 25, 20],
    "pl-top-right": [153, 0, 25, 20],
    "pl-left": [0, 42, 12, 29],
    "pl-right": [31, 42, 20, 29],
    "pl-bottom-left": [0, 72, 125, 38],
    "pl-bottom-tile": [179, 0, 25, 38],
    "pl-bottom-right": [126, 72, 150, 38],
  },
  "titlebar.bmp": {
    "main-title-bar": [27, 0, 275, 14],
  },
  "main.bmp": {
    "main-background": [0, 0, 275, 116],
  },
  "cbuttons.bmp": {
    "cb-previous": [0, 0, 23, 18],
    "cb-previous-pressed": [0, 18, 23, 18],
    "cb-play": [23, 0, 23, 18],
    "cb-play-pressed": [23, 18, 23, 18],
    "cb-pause": [46, 0, 23, 18],
    "cb-pause-pressed": [46, 18, 23, 18],
    "cb-stop": [69, 0, 23, 18],
    "cb-stop-pressed": [69, 18, 23, 18],
    "cb-next": [92, 0, 22, 18],
    "cb-next-pressed": [92, 18, 22, 18],
    "cb-eject": [114, 0, 22, 16],
    "cb-eject-pressed": [114, 16, 22, 16],
  },
  "shufrep.bmp": {
    "cb-shuffle": [28, 0, 47, 15],
    "cb-shuffle-pressed": [28, 15, 47, 15],
    "cb-repeat": [0, 0, 28, 15],
    "cb-repeat-pressed": [0, 15, 28, 15],
  },
};

// text.bmp is a 5x6 pixel font laid out in these three rows.
const FONT_ROWS = [
  "abcdefghijklmnopqrstuvwxyz\"@",
  "0123456789….:()-'!_+\\/[]^&%,=$#",
  "ÅÖÄ?*",
];
const GLYPH_WIDTH = 5;
const GLYPH_HEIGHT = 6;
// Winamp draws a space with the cell at column 30 of the first row; the cells
// between "@" and it hold other symbols in some skins.
const SPACE_POSITION = [30 * GLYPH_WIDTH, 0];
const MARQUEE_CHARACTERS = 31;
const MARQUEE_STEP_MS = 220;

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`Could not load ${url}`));
    image.src = url;
  });
}

function cropToDataUrl(image, [x, y, width, height]) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d").drawImage(image, x, y, width, height, 0, 0, width, height);
  return canvas.toDataURL();
}

function glyphPosition(character) {
  if (character === " ") {
    return SPACE_POSITION;
  }
  const lower = character.toLowerCase();
  for (let row = 0; row < FONT_ROWS.length; row++) {
    const column = FONT_ROWS[row].indexOf(lower);
    if (column !== -1) {
      return [column * GLYPH_WIDTH, row * GLYPH_HEIGHT];
    }
  }
  return SPACE_POSITION; // Unknown characters are drawn as spaces.
}

// Draws text onto a canvas with the skin's bitmap font, shown at double size.
function drawBitmapText(canvas, font, text) {
  canvas.width = text.length * GLYPH_WIDTH;
  canvas.height = GLYPH_HEIGHT;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  [...text].forEach((character, index) => {
    const position = glyphPosition(character);
    if (position) {
      context.drawImage(font, position[0], position[1], GLYPH_WIDTH, GLYPH_HEIGHT,
        index * GLYPH_WIDTH, 0, GLYPH_WIDTH, GLYPH_HEIGHT);
    }
  });
  if (!canvas.classList.contains("marquee")) {
    canvas.style.width = `${canvas.width * 2}px`;
  }
}

// Scrolls text through the main window's song-title area, like Winamp.
function startMarquee(canvas, font) {
  let text = canvas.dataset.text;
  while (text.length < MARQUEE_CHARACTERS) {
    text += text;
  }
  let start = 0;
  setInterval(() => {
    const doubled = text + text;
    drawBitmapText(canvas, font, doubled.slice(start, start + MARQUEE_CHARACTERS));
    start = (start + 1) % text.length;
  }, MARQUEE_STEP_MS);
}

// Pixels with every channel below this count as near-black. Most main windows
// have large black display areas, which would otherwise win the count.
const NEAR_BLACK_LIMIT = 48;

// Returns the skin's primary color: the most common color in its main window,
// as [red, green, blue], ignoring near-black unless the window is almost all
// black. Similar shades are counted together (16 levels per channel) and the
// result is their average, so the color is one the skin actually uses.
function primaryColor(image) {
  const canvas = document.createElement("canvas");
  canvas.width = 275;
  canvas.height = 116;
  const context = canvas.getContext("2d");
  context.drawImage(image, 0, 0, 275, 116, 0, 0, 275, 116);
  const pixels = context.getImageData(0, 0, 275, 116).data;

  const groups = new Map();
  for (let index = 0; index < pixels.length; index += 4) {
    const red = pixels[index];
    const green = pixels[index + 1];
    const blue = pixels[index + 2];
    const key = `${red >> 4},${green >> 4},${blue >> 4}`;
    const group = groups.get(key) || { count: 0, red: 0, green: 0, blue: 0 };
    group.count += 1;
    group.red += red;
    group.green += green;
    group.blue += blue;
    groups.set(key, group);
  }

  let largest = null;
  let largestColorful = null;
  for (const group of groups.values()) {
    const nearBlack = Math.max(group.red, group.green, group.blue) / group.count
      < NEAR_BLACK_LIMIT;
    if (largest === null || group.count > largest.count) {
      largest = group;
    }
    if (!nearBlack && (largestColorful === null || group.count > largestColorful.count)) {
      largestColorful = group;
    }
  }
  // Use the colorful choice unless it covers only a sliver of the window.
  const totalPixels = pixels.length / 4;
  if (largestColorful !== null && largestColorful.count >= totalPixels * 0.05) {
    largest = largestColorful;
  }
  return [largest.red, largest.green, largest.blue].map(
    (total) => Math.round(total / largest.count));
}

// WCAG relative luminance of an [red, green, blue] color.
function luminance(color) {
  const [red, green, blue] = color.map((value) => {
    const channel = value / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

// Colors the page behind the windows with the skin's primary color, with
// dark or light text, whichever contrasts more.
function colorPageBackground(mainImage) {
  const color = primaryColor(mainImage);
  const background = luminance(color);
  const contrastWithWhite = 1.05 / (background + 0.05);
  const contrastWithBlack = (background + 0.05) / 0.05;
  const bodyStyle = document.body.style;
  bodyStyle.setProperty("--page-bg", `rgb(${color.join(", ")})`);
  bodyStyle.setProperty("--page-text",
    contrastWithWhite >= contrastWithBlack ? "#ffffff" : "#111111");
}

async function applySkin() {
  const skinUrl = document.body.dataset.skinUrl;
  if (!skinUrl) {
    return; // "No skin" chosen.
  }
  const rootStyle = document.documentElement.style;
  for (const [sheet, sprites] of Object.entries(SPRITES)) {
    try {
      const image = await loadImage(skinUrl + sheet);
      for (const [name, box] of Object.entries(sprites)) {
        rootStyle.setProperty(`--sprite-${name}`, `url(${cropToDataUrl(image, box)})`);
      }
      if (sheet === "main.bmp") {
        colorPageBackground(image);
      }
    } catch (error) {
      console.warn(error); // The page still works without this part of the skin.
    }
  }
  try {
    const font = await loadImage(skinUrl + "text.bmp");
    document.querySelectorAll(".bitmap-text:not(.marquee)").forEach((canvas) => {
      drawBitmapText(canvas, font, canvas.dataset.text);
    });
    document.querySelectorAll(".marquee").forEach((canvas) => startMarquee(canvas, font));
  } catch (error) {
    console.warn(error);
  }
}

// The main window's transport buttons drive the session filters.
function stepDate(direction) {
  const select = document.getElementById("date");
  const lastIndex = select.options.length - 1; // Index 0 is "All".
  let index = select.selectedIndex + direction;
  if (index < 1) {
    index = lastIndex;
  } else if (index > lastIndex) {
    index = 1;
  }
  select.selectedIndex = index;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function handleTransportButton(action) {
  const searchForm = document.getElementById("search-form");
  if (action === "rebuild") {
    document.getElementById("rebuild-form").requestSubmit();
    return;
  }
  if (!searchForm) {
    return; // No session data yet.
  }
  if (action === "previous-day") {
    stepDate(-1);
  } else if (action === "next-day") {
    stepDate(1);
  } else if (action === "clear") {
    searchForm.reset(); // Venue, date and search back to empty.
    searchForm.requestSubmit();
  } else if (action === "refresh") {
    searchForm.requestSubmit(); // Same filters and search, fresh results.
  }
}

// Keeps each action button in a selection form ("Add N selected to
// favorites", "Book N selected", ...) in step with the ticked sessions.
// A button with data-max allows at most that many at once.
function updateSelectionButtons(form) {
  const selected = form.querySelectorAll("input[name=session_ids]:checked").length;
  const signedIn = form.querySelector(".actions").dataset.signedIn === "yes";
  form.querySelectorAll(".selection-action").forEach((button) => {
    const limit = button.dataset.max ? Number(button.dataset.max) : Infinity;
    button.disabled = !signedIn || selected === 0 || selected > limit;
    if (selected > limit) {
      button.textContent = `Select at most ${limit} to use this`;
    } else {
      button.textContent = button.dataset.label.replace("{count}", selected);
    }
  });
}

// Shows the page's own confirm dialog and resolves to true if OK was chosen.
function askToConfirm(question) {
  const dialog = document.getElementById("confirm-dialog");
  dialog.querySelector(".confirm-message").textContent = question;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"),
      { once: true });
  });
}

// HTMX asks before every request; take over the ones that have hx-confirm.
function confirmWithDialog(event) {
  if (!event.detail.question) {
    return;
  }
  event.preventDefault();
  askToConfirm(event.detail.question).then((confirmed) => {
    if (confirmed) {
      event.detail.issueRequest(true); // true: don't ask again.
    }
  });
}

// The desktop app (events_desktop.py) shows the page in a window without a
// frame, so it adds minimize and close buttons. With a skin they sit over the
// buttons drawn in the skin's title bar; without one they show at the top.
const WINDOW_ACTIONS = [["minimize", "Minimize", "–"], ["close", "Close", "×"]];

function addWindowControls() {
  if (document.querySelector(".window-controls")) {
    return;
  }
  const controls = document.createElement("div");
  controls.className = "window-controls";
  for (const [action, label, symbol] of WINDOW_ACTIONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.textContent = symbol;
    button.addEventListener("click", () => window.pywebview.api[action]());
    controls.append(button);
  }
  const mainWindow = document.querySelector(".main-window");
  (mainWindow || document.body).append(controls);
}

// pywebview fires this once window.pywebview.api is ready to use.
window.addEventListener("pywebviewready", addWindowControls);

document.addEventListener("DOMContentLoaded", () => {
  document.body.addEventListener("htmx:confirm", confirmWithDialog);
  applySkin();
  document.querySelectorAll(".cbutton").forEach((button) => {
    button.addEventListener("click", () => handleTransportButton(button.dataset.action));
  });
  document.addEventListener("change", (event) => {
    const form = event.target.closest(".selection-form");
    if (event.target.name === "session_ids" && form) {
      updateSelectionButtons(form);
    }
  });
});
