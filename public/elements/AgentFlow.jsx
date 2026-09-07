import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import {
  AlertTriangle,
  Calculator,
  CheckCircle2,
  Circle,
  FileOutput,
  FileText,
  LoaderCircle,
  MinusCircle,
  PauseCircle,
  Search,
  ShieldCheck,
  UserCheck,
} from "lucide-react";

const nodeIcons = {
  document: FileText,
  financial: Calculator,
  research: Search,
  risk: ShieldCheck,
  approval: UserCheck,
  report: FileOutput,
};

const stateView = {
  pending: {
    icon: Circle,
    badge: "secondary",
    card: "border-border bg-card",
    iconClass: "text-muted-foreground",
  },
  running: {
    icon: LoaderCircle,
    badge: "default",
    card: "border-primary bg-primary/5 shadow-sm",
    iconClass: "text-primary animate-spin",
  },
  done: {
    icon: CheckCircle2,
    badge: "secondary",
    card: "border-emerald-500/40 bg-emerald-500/5",
    iconClass: "text-emerald-600 dark:text-emerald-400",
  },
  waiting: {
    icon: PauseCircle,
    badge: "outline",
    card: "border-amber-500/60 bg-amber-500/10 shadow-sm",
    iconClass: "text-amber-600 dark:text-amber-400",
  },
  skipped: {
    icon: MinusCircle,
    badge: "outline",
    card: "border-border bg-muted/30",
    iconClass: "text-muted-foreground",
  },
  failed: {
    icon: AlertTriangle,
    badge: "destructive",
    card: "border-destructive/60 bg-destructive/10 shadow-sm",
    iconClass: "text-destructive",
  },
};

function WorkflowNode({ node }) {
  const view = stateView[node.status] || stateView.pending;
  const StateIcon = view.icon;
  const NodeIcon = nodeIcons[node.id] || Circle;

  return (
    <div className={`min-w-0 rounded-lg border p-3 ${view.card}`}>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <NodeIcon className="h-4 w-4 shrink-0 text-foreground" />
          <span className="truncate text-sm font-semibold">{node.label}</span>
        </div>
        <StateIcon className={`h-4 w-4 shrink-0 ${view.iconClass}`} />
      </div>
      <p className="mb-3 min-h-10 text-xs leading-5 text-muted-foreground">
        {node.description}
      </p>
      <Badge variant={view.badge}>{node.statusLabel}</Badge>
    </div>
  );
}

export default function AgentFlow() {
  const model = props || {};
  const progress = model.progress || { processed: 0, total: 6, percent: 0 };
  const nodes = Array.isArray(model.nodes) ? model.nodes : [];
  const nextNodes = Array.isArray(model.nextNodes) ? model.nextNodes : [];

  return (
    <Card className="my-3 w-full overflow-hidden">
      <CardHeader className="space-y-3 pb-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Agent 执行流程</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              {model.caseId || "—"} · Thread {model.threadId || "—"}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge variant="outline">{model.workflowStatusLabel || "UNKNOWN"}</Badge>
            <Badge variant="secondary">风险 {model.riskLevel || "—"}</Badge>
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>已处理 {progress.processed}/{progress.total}</span>
            <span>{progress.percent}%</span>
          </div>
          <Progress value={progress.percent} />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {nodes.map((node) => <WorkflowNode key={node.id} node={node} />)}
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 border-t pt-3 text-xs text-muted-foreground">
          <span>当前节点：{model.currentNode || "无"}</span>
          <span>下一节点：{nextNodes.length ? nextNodes.join("、") : "无"}</span>
          <span>Run：{model.runId || "legacy"}</span>
        </div>
      </CardContent>
    </Card>
  );
}
