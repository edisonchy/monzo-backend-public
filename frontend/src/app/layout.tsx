import type { Metadata, Viewport } from "next";
import "./tokens.css";
import "./dashboard.css";
import "./spending.css";
import "./dashboard-system.css";
export const metadata: Metadata = {
  title: "Personal finance",
  robots: { index: false, follow: false },
};
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#111113",
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-GB">
      <body>{children}</body>
    </html>
  );
}
