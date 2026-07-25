import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "next-lab",
  description: "Personal Next.js playground",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <Link href="/">Home</Link>
          <Link href="/notes">Notes</Link>
        </nav>
        <main className="main">{children}</main>
      </body>
    </html>
  );
}
