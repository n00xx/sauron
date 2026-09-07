import React from "react";

interface IconProps {
  size?: number;
  color?: string;
}

/** SVG en linea: se mantiene nitido a cualquier escala y no depende de fuentes de iconos. */
const svg = (path: React.ReactNode) => {
  const Icon: React.FC<IconProps> = ({ size = 44, color = "currentColor" }) => (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {path}
    </svg>
  );
  return Icon;
};

export const IconPhone = svg(
  <>
    <rect x="6" y="2" width="12" height="20" rx="2.5" />
    <path d="M11 18.5h2" />
  </>
);

export const IconLaptop = svg(
  <>
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M2 20h20" />
  </>
);

export const IconTv = svg(
  <>
    <rect x="2" y="4" width="20" height="14" rx="2" />
    <path d="M8 21h8M12 18v3" />
  </>
);

export const IconGamepad = svg(
  <>
    <path d="M6 12h4M8 10v4" />
    <circle cx="15.5" cy="11" r="1" />
    <circle cx="18" cy="13.5" r="1" />
    <rect x="2" y="6" width="20" height="12" rx="5" />
  </>
);

export const IconDownload = svg(
  <>
    <path d="M12 3v12M7.5 10.5 12 15l4.5-4.5" />
    <path d="M4 20h16" />
  </>
);

export const IconCheck = svg(<path d="M4.5 12.5 9.5 17.5 19.5 7" />);

export const IconTablet = svg(
  <>
    <rect x="4" y="2.5" width="16" height="19" rx="2.5" />
    <path d="M10.5 18.5h3" />
  </>
);

export const IconBook = svg(
  <>
    <path d="M4 4.5A2 2 0 0 1 6 3h13v15H6a2 2 0 0 0-2 2z" />
    <path d="M4 18.5V21h15" />
  </>
);
