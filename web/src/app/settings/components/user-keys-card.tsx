"use client";

import { useEffect, useRef, useState } from "react";
import { Ban, CheckCircle2, Coins, Copy, KeyRound, LoaderCircle, Pencil, Plus, RotateCcw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { createUserKey, deleteUserKey, fetchUserKeys, updateUserKey, type UserKey, type UserQuotaMode } from "@/lib/api";

function formatDateTime(value?: string | null) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function QuotaModeSelector({ value, onChange }: { value: UserQuotaMode; onChange: (value: UserQuotaMode) => void }) {
  return (
    <div className="grid grid-cols-2 gap-1 rounded-xl bg-stone-100 p-1" role="radiogroup" aria-label="额度模式">
      {([
        ["dynamic", "动态额度"],
        ["fixed", "独立额度"],
      ] as const).map(([mode, label]) => (
        <button
          key={mode}
          type="button"
          role="radio"
          aria-checked={value === mode}
          onClick={() => onChange(mode)}
          className={`h-9 rounded-lg px-3 text-sm font-medium transition ${
            value === mode ? "bg-white text-stone-950 shadow-sm" : "text-stone-500 hover:text-stone-800"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function UserKeysCard() {
  const didLoadRef = useRef(false);
  const [items, setItems] = useState<UserKey[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [quotaMode, setQuotaMode] = useState<UserQuotaMode>("fixed");
  const [quotaTotal, setQuotaTotal] = useState("100");
  const [isCreating, setIsCreating] = useState(false);
  const [pendingIds, setPendingIds] = useState<Set<string>>(() => new Set());
  const [revealedKey, setRevealedKey] = useState("");
  const [deletingItem, setDeletingItem] = useState<UserKey | null>(null);
  const [editingItem, setEditingItem] = useState<UserKey | null>(null);
  const [editName, setEditName] = useState("");
  const [editKey, setEditKey] = useState("");
  const [editQuotaMode, setEditQuotaMode] = useState<UserQuotaMode>("fixed");
  const [editQuotaTotal, setEditQuotaTotal] = useState("0");

  const parseQuota = (value: string) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : null;
  };

  const load = async () => {
    setIsLoading(true);
    try {
      const data = await fetchUserKeys();
      setItems(data.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载用户密钥失败");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void load();
  }, []);

  const handleCreate = async () => {
    const parsedQuota = parseQuota(quotaTotal);
    if (quotaMode === "fixed" && parsedQuota === null) {
      toast.error("请输入有效的用户总额度");
      return;
    }
    setIsCreating(true);
    try {
      const data = await createUserKey(name.trim(), parsedQuota ?? 0, quotaMode);
      setItems(data.items);
      setRevealedKey(data.key);
      setName("");
      setIsDialogOpen(false);
      toast.success("用户密钥已创建");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建用户密钥失败");
    } finally {
      setIsCreating(false);
    }
  };

  const setItemPending = (id: string, isPending: boolean) => {
    setPendingIds((current) => {
      const next = new Set(current);
      if (isPending) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  };

  const handleToggle = async (item: UserKey) => {
    setItemPending(item.id, true);
    try {
      const data = await updateUserKey(item.id, { enabled: !item.enabled });
      setItems(data.items);
      toast.success(item.enabled ? "用户密钥已禁用" : "用户密钥已启用");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新用户密钥失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const handleDelete = async () => {
    if (!deletingItem) {
      return;
    }
    const item = deletingItem;
    setItemPending(item.id, true);
    try {
      const data = await deleteUserKey(item.id);
      setItems(data.items);
      setDeletingItem(null);
      toast.success("用户密钥已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除用户密钥失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const openEditDialog = (item: UserKey) => {
    setEditingItem(item);
    setEditName(item.name);
    setEditKey("");
    setEditQuotaMode(item.quota_mode);
    setEditQuotaTotal(String(item.quota_total));
  };

  const handleEdit = async () => {
    if (!editingItem) {
      return;
    }
    const item = editingItem;
    const trimmedName = editName.trim();
    const trimmedKey = editKey.trim();
    const parsedQuota = parseQuota(editQuotaTotal);
    if (editQuotaMode === "fixed" && parsedQuota === null) {
      toast.error("请输入有效的用户总额度");
      return;
    }
    const nextQuotaTotal = parsedQuota ?? item.quota_total;
    if (trimmedName === item.name && !trimmedKey && editQuotaMode === item.quota_mode && nextQuotaTotal === item.quota_total) {
      setEditingItem(null);
      return;
    }
    setItemPending(item.id, true);
    try {
      const data = await updateUserKey(item.id, {
        ...(trimmedName !== item.name ? { name: trimmedName } : {}),
        ...(trimmedKey ? { key: trimmedKey } : {}),
        ...(editQuotaMode !== item.quota_mode ? { quota_mode: editQuotaMode } : {}),
        ...(nextQuotaTotal !== item.quota_total ? { quota_total: nextQuotaTotal } : {}),
      });
      setItems(data.items);
      setEditingItem(null);
      setEditKey("");
      toast.success(trimmedKey ? "用户密钥已更新" : "用户信息已更新");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新用户密钥失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const handleResetUsage = async () => {
    if (!editingItem) {
      return;
    }
    const item = editingItem;
    setItemPending(item.id, true);
    try {
      const data = await updateUserKey(item.id, { reset_quota_used: true });
      setItems(data.items);
      setEditingItem(data.item);
      setEditQuotaTotal(String(data.item.quota_total));
      toast.success("用户已用额度已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置用户额度失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const handleCopy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动复制");
    }
  };

  return (
    <>
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-6 p-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
                <KeyRound className="size-5 text-stone-600" />
              </div>
              <div>
                <h2 className="text-lg font-semibold tracking-tight">用户与配额管理</h2>
                <p className="text-sm text-stone-500">创建普通用户密钥并选择动态同步或独立生图额度；普通用户只能进入画图页。</p>
              </div>
            </div>
            <Button className="h-9 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800" onClick={() => setIsDialogOpen(true)}>
              <Plus className="size-4" />
              创建用户密钥
            </Button>
          </div>

          {revealedKey ? (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm text-emerald-900">
              <div className="font-medium">新密钥仅展示一次，请立即保存：</div>
              <div className="mt-3 flex flex-col gap-3 rounded-lg border border-emerald-200 bg-white/80 p-3 md:flex-row md:items-center md:justify-between">
                <code className="break-all font-mono text-[13px]">{revealedKey}</code>
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-emerald-200 bg-white px-4 text-emerald-700"
                  onClick={() => void handleCopy(revealedKey)}
                >
                  <Copy className="size-4" />
                  复制
                </Button>
              </div>
            </div>
          ) : null}

          {isLoading ? (
            <div className="flex items-center justify-center py-10">
              <LoaderCircle className="size-5 animate-spin text-stone-400" />
            </div>
          ) : items.length === 0 ? (
            <div className="rounded-xl bg-stone-50 px-6 py-10 text-center text-sm text-stone-500">
              暂无普通用户密钥。点击右上角按钮后即可创建并分发给其他人。
            </div>
          ) : (
            <div className="space-y-3">
              {items.map((item) => {
                const isPending = pendingIds.has(item.id);
                return (
                  <div key={item.id} className="flex flex-col gap-3 rounded-xl border border-stone-200 bg-white px-4 py-4 md:flex-row md:items-center md:justify-between">
                    <div className="min-w-0 space-y-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="truncate text-sm font-medium text-stone-800">{item.name}</div>
                        <Badge variant={item.enabled ? "success" : "secondary"} className="rounded-md">
                          {item.enabled ? "已启用" : "已禁用"}
                        </Badge>
                        <Badge variant="secondary" className="rounded-md">
                          {item.quota_mode === "dynamic" ? "动态同步" : "独立额度"}
                        </Badge>
                      </div>
                      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500">
                        <span className="inline-flex items-center gap-1 font-medium text-stone-700">
                          <Coins className="size-3.5" />
                          {item.quota_mode === "dynamic"
                            ? `动态额度 ${item.quota_remaining}`
                            : `剩余 ${item.quota_remaining} / 总额 ${item.quota_total}`}
                        </span>
                        {item.quota_mode === "fixed" ? <span>已用 {item.quota_used}</span> : null}
                        <span>创建时间 {formatDateTime(item.created_at)}</span>
                        <span>最近使用 {formatDateTime(item.last_used_at)}</span>
                      </div>
                    </div>

                    <div className="flex items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                        onClick={() => openEditDialog(item)}
                        disabled={isPending}
                      >
                        {isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Pencil className="size-4" />}
                        编辑
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                        onClick={() => void handleToggle(item)}
                        disabled={isPending}
                      >
                        {isPending ? (
                          <LoaderCircle className="size-4 animate-spin" />
                        ) : item.enabled ? (
                          <Ban className="size-4" />
                        ) : (
                          <CheckCircle2 className="size-4" />
                        )}
                        {item.enabled ? "禁用" : "启用"}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-rose-200 bg-white px-4 text-rose-600 hover:bg-rose-50 hover:text-rose-700"
                        onClick={() => setDeletingItem(item)}
                        disabled={isPending}
                      >
                        {isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                        删除
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>创建用户密钥</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              可选填写一个备注名称，方便区分不同使用者；创建后会生成一条只能查看一次的原始密钥。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">名称（可选）</label>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：设计同学 A、运营临时账号"
              className="h-11 rounded-xl border-stone-200 bg-white"
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">额度模式</label>
            <QuotaModeSelector value={quotaMode} onChange={setQuotaMode} />
            <p className="text-xs leading-5 text-stone-500">
              {quotaMode === "dynamic" ? "与管理员画图页的后台账号额度保持一致。" : "使用单独设置的额度，不随后台账号额度刷新。"}
            </p>
          </div>
          {quotaMode === "fixed" ? (
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">初始总额度</label>
              <Input
                type="number"
                min="0"
                step="1"
                value={quotaTotal}
                onChange={(event) => setQuotaTotal(event.target.value)}
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
              <p className="text-xs leading-5 text-stone-500">普通用户每提交一张图片消耗 1 点额度。</p>
            </div>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setIsDialogOpen(false)}
              disabled={isCreating}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleCreate()}
              disabled={isCreating}
            >
              {isCreating ? <LoaderCircle className="size-4 animate-spin" /> : <Plus className="size-4" />}
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(deletingItem)} onOpenChange={(open) => (!open ? setDeletingItem(null) : null)}>
        <DialogContent className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>删除用户密钥</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              确认删除用户密钥「{deletingItem?.name}」吗？删除后该密钥将无法继续调用接口。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setDeletingItem(null)}
              disabled={deletingItem ? pendingIds.has(deletingItem.id) : false}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-rose-600 px-5 text-white hover:bg-rose-700"
              onClick={() => void handleDelete()}
              disabled={deletingItem ? pendingIds.has(deletingItem.id) : false}
            >
              {deletingItem && pendingIds.has(deletingItem.id) ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(editingItem)}
        onOpenChange={(open) => {
          if (!open) {
            setEditingItem(null);
            setEditKey("");
          }
        }}
      >
        <DialogContent className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>编辑用户密钥</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              可以修改备注名称；如需更换专用密钥，直接填写新的原始密钥即可。留空则保持当前密钥不变。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">名称</label>
              <Input
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
                placeholder="例如：设计同学 A、运营临时账号"
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">新的专用密钥（可选）</label>
              <Input
                value={editKey}
                onChange={(event) => setEditKey(event.target.value)}
                placeholder="例如：sk-your-custom-user-key"
                className="h-11 rounded-xl border-stone-200 bg-white font-mono"
              />
              <p className="text-xs leading-5 text-stone-500">
                保存后旧密钥会立即失效，新密钥生效。系统仍只保存哈希，不会回显当前密钥。
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">额度模式</label>
              <QuotaModeSelector value={editQuotaMode} onChange={setEditQuotaMode} />
              <p className="text-xs leading-5 text-stone-500">
                {editQuotaMode === "dynamic" ? "与管理员画图页的后台账号额度保持一致。" : "使用单独设置的额度，不随后台账号额度刷新。"}
              </p>
            </div>
            {editQuotaMode === "fixed" ? (
              <div className="space-y-2">
                <label className="text-sm font-medium text-stone-700">总额度</label>
                <Input
                  type="number"
                  min="0"
                  step="1"
                  value={editQuotaTotal}
                  onChange={(event) => setEditQuotaTotal(event.target.value)}
                  className="h-11 rounded-xl border-stone-200 bg-white"
                />
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-stone-500">
                  <span>已用 {editingItem?.quota_used ?? 0}，剩余 {editingItem?.quota_remaining ?? 0}</span>
                  <Button
                    type="button"
                    variant="outline"
                    className="h-8 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                    onClick={() => void handleResetUsage()}
                    disabled={editingItem ? pendingIds.has(editingItem.id) : false}
                  >
                    <RotateCcw className="size-3.5" />
                    重置已用额度
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => {
                setEditingItem(null);
                setEditKey("");
              }}
              disabled={editingItem ? pendingIds.has(editingItem.id) : false}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleEdit()}
              disabled={editingItem ? pendingIds.has(editingItem.id) : false}
            >
              {editingItem && pendingIds.has(editingItem.id) ? <LoaderCircle className="size-4 animate-spin" /> : <Pencil className="size-4" />}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
