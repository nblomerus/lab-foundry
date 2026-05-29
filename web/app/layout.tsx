import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { SiteNav } from "./components/SiteNav";
import { PageNav } from "./components/PageNav";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "LabFoundry — Research Lab Command Center",
  description: "Autonomous AI-native research laboratory discovering, validating, and publishing.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-screen text-slate-950" suppressHydrationWarning>
        <SiteNav />
        <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 xl:pl-28">
          <PageNav />
          {children}
        </main>
      </body>
    </html>
  );
}
