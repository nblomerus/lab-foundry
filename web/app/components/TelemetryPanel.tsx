"use client";

import { Activity, Database, Gauge, Zap } from "lucide-react";
import {
  Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { Cost, TelemetryDay } from "../lib/types";
import { Badge, Card, SectionTitle } from "./ui";

export function TelemetryPanel({
  telemetry, cost, totalRuns, savedFindings,
}: {
  telemetry: TelemetryDay[];
  cost: Cost;
  totalRuns: number;
  savedFindings: number;
}) {
  const tokensK = telemetry.reduce((sum, t) => sum + t.tokens, 0);

  return (
    <Card className="lg:col-span-6">
      <SectionTitle
        icon={Gauge}
        title="Run telemetry"
        subtitle="Local inference isn't free — track time, electricity, and useful output density."
      />
      <div className="grid gap-4 md:grid-cols-3">
        <div className="rounded-3xl bg-slate-50 p-4">
          <div className="flex items-center justify-between">
            <Activity className="h-4 w-4 text-slate-500" />
            <Badge tone={cost.cap_reached ? "amber" : "green"}>
              {cost.cap_reached ? "Cap reached" : "Healthy"}
            </Badge>
          </div>
          <div className="mt-4 text-3xl font-semibold tracking-tight">{totalRuns}</div>
          <div className="text-sm text-slate-500">agent runs (recent)</div>
        </div>
        <div className="rounded-3xl bg-slate-50 p-4">
          <div className="flex items-center justify-between">
            <Database className="h-4 w-4 text-slate-500" />
            <Badge>Postgres</Badge>
          </div>
          <div className="mt-4 text-3xl font-semibold tracking-tight">{savedFindings}</div>
          <div className="text-sm text-slate-500">saved findings</div>
        </div>
        <div className="rounded-3xl bg-slate-50 p-4">
          <div className="flex items-center justify-between">
            <Zap className="h-4 w-4 text-slate-500" />
            <Badge tone="blue">Ollama</Badge>
          </div>
          <div className="mt-4 text-3xl font-semibold tracking-tight">{tokensK}K</div>
          <div className="text-sm text-slate-500">tokens this week</div>
        </div>
      </div>

      <div className="mt-5 h-56">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={telemetry} margin={{ top: 10, right: 8, bottom: 0, left: -20 }}>
            <XAxis dataKey="label" tickLine={false} axisLine={false} fontSize={12} />
            <YAxis tickLine={false} axisLine={false} fontSize={12} />
            <Tooltip />
            <Area type="monotone" dataKey="runs"     stroke="#0f172a" fill="#0f172a" fillOpacity={0.12} />
            <Area type="monotone" dataKey="findings" stroke="#64748b" fill="#64748b" fillOpacity={0.10} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
