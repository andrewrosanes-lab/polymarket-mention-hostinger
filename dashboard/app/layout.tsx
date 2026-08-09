import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const incoming = await headers();
  const host = incoming.get("x-forwarded-host") || incoming.get("host") || "72.60.77.17:3000";
  const protocol = incoming.get("x-forwarded-proto")
    || (/^(localhost|127\.|\d+\.)/.test(host) ? "http" : "https");
  const image = `${protocol}://${host}/og.png`;
  return {
    title: "Mention Edge — Polymarket Console",
    description: "Separated probability, timing, edge, and exposure for live mention markets",
    openGraph: {
      title: "Mention Edge — Polymarket Console",
      description: "Probability, timing, edge, directional routing, and arbitrage monitoring",
      images: [{ url: image, width: 1200, height: 630 }],
    },
    twitter: {
      card: "summary_large_image",
      title: "Mention Edge — Polymarket Console",
      description: "Probability, timing, edge, and exposure",
      images: [image],
    },
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
