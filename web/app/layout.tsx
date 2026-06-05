import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { TopBar } from "./components/TopBar";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "LabFoundry — Research OS",
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
        <div className="mx-auto max-w-[1760px] px-3 py-3 sm:px-5">
          <TopBar />
          <main className="mt-4">{children}</main>
        </div>
      </body>
    </html>
  );
}
