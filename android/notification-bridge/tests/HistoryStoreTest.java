package org.echo.accounts.notifications;

import java.io.File;
import java.nio.file.Files;
import java.util.List;

/** Offline fixtures only; no device, SDK, notification content or user configuration. */
public final class HistoryStoreTest {
    private static void check(boolean value) { if (!value) throw new AssertionError(); }
    private static String hash(String value) { return NotificationRules.hash(value); }
    public static void main(String[] args) throws Exception {
        File root = new File(args[0]);
        long now = HistoryStore.RETENTION_MS * 3;
        String target = hash("测试目标"), key = hash("key");
        check(NotificationRules.classify(false, false, "测试目标", null, target) == 0);
        check(NotificationRules.classify(false, false, "其他标题", "测试目标", target) == 0);
        check(NotificationRules.classify(false, false, "测试目标", "其他发送人", target) == 3);
        check(NotificationRules.classify(false, false, "测试目标 ", null, target) == 3);
        check(NotificationRules.classify(true, false, "测试目标", null, target) == 1);
        check(NotificationRules.classify(false, true, "测试目标", null, target) == 2);
        check(NotificationRules.classify(false, false, "测试目标", null, "") == 3);

        File file = new File(root, "history.bin");
        HistoryStore store = new HistoryStore(file, now);
        check(!store.add(key, hash("v1"), "标题", "内容", now, false, now));
        store.setTarget(target);
        check(store.add(key, hash("v1"), "标题", "内容", now - 2, false, now));
        check(!store.add(key, hash("v1"), "标题", "内容", now - 2, false, now));
        check(store.add(key, hash("v2"), "标题", "更新", now - 1, false, now));
        check(store.snapshot(now).size() == 2);
        store = new HistoryStore(file, now); // process/app restart
        check(store.target().equals(target) && !store.storageError());
        check(store.snapshot(now).get(0).text.equals("更新"));
        check(!store.add(key, hash("v2"), "标题", "更新", now - 1, true, now));
        store.clear();
        store = new HistoryStore(file, now);
        check(store.snapshot(now).isEmpty());
        check(!store.add(key, hash("v2"), "标题", "更新", now - 1, true, now));
        check(store.add(key, hash("v3"), "标题", "新消息", now, false, now));
        store.setTarget(hash("新的目标"));
        check(store.snapshot(now).isEmpty());
        check(store.add(key, hash("v3"), "标题", "新消息", now, true, now));
        check(store.snapshot(now).get(0).active);

        check(!store.add(hash("old"), hash("old"), "标题", "", now - HistoryStore.RETENTION_MS - 1, true, now));
        check(!store.add(hash("future"), hash("future"), "标题", "", now + 1, true, now));
        for (int i = 0; i < 110; i++) {
            store.add(hash("key" + i), hash("v" + i), "标题", "正文", now + i, false, now + i);
        }
        List<HistoryStore.Entry> entries = store.snapshot(now + 110);
        check(entries.size() == 100 && entries.get(0).time == now + 109);
        check(entries.get(99).time == now + 10);
        store = new HistoryStore(file, now + HistoryStore.RETENTION_MS + 110);
        check(store.snapshot(now + HistoryStore.RETENTION_MS + 110).isEmpty());
        store = new HistoryStore(file, now + HistoryStore.RETENTION_MS + 110);
        check(store.snapshot(now + HistoryStore.RETENTION_MS + 110).isEmpty());
        store.setTarget(target);
        StringBuilder longBody = new StringBuilder();
        for (int i = 0; i < 5000; i++) longBody.append("字");
        store.add(key, hash("long"), longBody.toString(), longBody.toString(), now, false, now);
        check(store.snapshot(now).get(0).title.length() == 256);
        check(store.snapshot(now).get(0).text.length() == 4000);
        String clipped = HistoryStore.clip("ab\uD83D\uDE00cd", 4);
        check(clipped.equals("ab…"));

        Files.write(file.toPath(), new byte[]{0, 1, 2});
        store = new HistoryStore(file, now);
        check(store.storageError() && store.target().isEmpty() && store.snapshot(now).isEmpty());
        // A failed write stays visible and never passes for durable state.
        store = new HistoryStore(new File(new File(root, "missing"), "history.bin"), now);
        store.setTarget(target);
        check(store.storageError());
        System.out.println("notification rules and history scenarios passed");
    }
}
