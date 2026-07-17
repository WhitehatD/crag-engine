// Hand-drawn minimal SVG icon set. No icon font, no library — each is a 16x16
// stroked glyph inheriting currentColor. Kept tiny and consistent (1.5 stroke,
// round caps) to match the flat crag aesthetic.

type P = { size?: number; className?: string };

function svg(size: number, className: string | undefined, children: React.ReactNode) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      {children}
    </svg>
  );
}

// Loop — a cycle arrow.
export const IconLoop = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M13 8a5 5 0 1 1-1.5-3.5" />
      <path d="M13 2v3h-3" />
    </>
  ));

// Claims — a document with a check.
export const IconClaims = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M4 2h5l3 3v9H4z" />
      <path d="M6.5 9.5 8 11l2.5-3" />
    </>
  ));

// Review — an inbox tray.
export const IconReview = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M2 9v4h12V9l-2-6H4z" />
      <path d="M2 9h3l1 2h4l1-2h3" />
    </>
  ));

// Grounding — a target.
export const IconGrounding = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <circle cx="8" cy="8" r="6" />
      <circle cx="8" cy="8" r="2.5" />
    </>
  ));

// Corpus — stacked layers.
export const IconCorpus = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M8 2 2 5l6 3 6-3z" />
      <path d="M2 8l6 3 6-3" />
      <path d="M2 11l6 3 6-3" />
    </>
  ));

// Sessions — a clock.
export const IconSessions = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 5v3l2 1.5" />
    </>
  ));

// Help / glossary — a question mark in a circle.
export const IconHelp = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M6.2 6.2a1.8 1.8 0 1 1 2.6 1.7c-.6.4-.8.7-.8 1.3" />
      <path d="M8 11.6h.01" />
    </>
  ));

// Refresh — circular arrows.
export const IconRefresh = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M13 8a5 5 0 0 1-8.5 3.5" />
      <path d="M3 8a5 5 0 0 1 8.5-3.5" />
      <path d="M11.5 2v2.5H9" />
      <path d="M4.5 14v-2.5H7" />
    </>
  ));

// Close — an X.
export const IconClose = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M4 4l8 8M12 4l-8 8" />
    </>
  ));

// Chevron down — for expandable panels.
export const IconChevron = ({ size = 16, className }: P) =>
  svg(size, className, <path d="M4 6l4 4 4-4" />);

// External link — for the "powered by crag" badge.
export const IconExternal = ({ size = 16, className }: P) =>
  svg(size, className, (
    <>
      <path d="M6 3H3v10h10v-3" />
      <path d="M9 3h4v4" />
      <path d="M13 3l-6 6" />
    </>
  ));
