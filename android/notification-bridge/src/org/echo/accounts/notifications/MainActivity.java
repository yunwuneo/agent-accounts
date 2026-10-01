package org.echo.accounts.notifications;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.NotificationManager;
import android.content.ComponentName;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.service.notification.NotificationListenerService;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.view.WindowManager;
import android.graphics.Color;
import android.graphics.Typeface;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

public final class MainActivity extends Activity {
    private HistoryStore history;
    private TextView status, heading;
    private LinearLayout rows;
    private Button clear;
    private long rendered = -1;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override public void run() { render(); handler.postDelayed(this, 1000); }
    };

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView text(String value, int size, int color) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setTextColor(color);
        view.setPadding(0, dp(6), 0, dp(6));
        return view;
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        // Keep private notification text out of screenshots and the recents preview.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        history = BridgeState.history(this);
        ComponentName listener = new ComponentName(this, Listener.class);
        if (getSystemService(NotificationManager.class).isNotificationListenerAccessGranted(listener)) {
            // An explicit app launch can restore an existing grant after APK updates.
            // Never grant access or automatically restart the PC monitor.
            NotificationListenerService.requestRebind(listener);
        }
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(20), 0, dp(20), 0);
        layout.setBackgroundColor(Color.rgb(248, 250, 252));
        layout.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(dp(20), insets.getSystemWindowInsetTop() + dp(12),
                            dp(20), insets.getSystemWindowInsetBottom() + dp(12));
            return insets;
        });
        TextView title = text("Echo 通知桥", 26, Color.rgb(15, 23, 42));
        title.setTypeface(null, Typeface.BOLD);
        layout.addView(title);
        status = text("", 14, Color.rgb(30, 64, 175));
        layout.addView(status);
        Button button = new Button(this);
        button.setText("打开通知使用权设置");
        button.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));
        layout.addView(button);
        layout.addView(text("只记录抖音中精确匹配目标昵称的通知，排除汇总和明确群聊。目标由电脑连接配置，配置后断开电脑仍可记录。", 13, Color.DKGRAY));
        heading = text("符合条件的通知历史", 18, Color.rgb(15, 23, 42));
        heading.setTypeface(null, Typeface.BOLD);
        layout.addView(heading);
        LinearLayout actions = new LinearLayout(this);
        Button update = new Button(this);
        update.setText("刷新");
        update.setOnClickListener(v -> { rendered = -1; render(); });
        actions.addView(update, new LinearLayout.LayoutParams(0, -2, 1));
        clear = new Button(this);
        clear.setText("清空历史");
        clear.setOnClickListener(v -> new AlertDialog.Builder(this)
            .setTitle("清空通知历史？")
            .setMessage("只删除本应用的历史，不删除抖音消息或系统通知。后续符合条件的通知仍会记录。")
            .setNegativeButton("取消", null)
            .setPositiveButton("清空", (dialog, which) -> { history.clear(); render(); }).show());
        actions.addView(clear, new LinearLayout.LayoutParams(0, -2, 1));
        layout.addView(actions);
        ScrollView scroll = new ScrollView(this);
        rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(rows);
        layout.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        layout.addView(text("仅存手机应用内 · 最近100条 / 7天\n正文不传电脑，无网络权限。仅显示开始监听后收到的通知及接入时仍在通知栏的通知，无法恢复此前已消失的通知。", 12, Color.DKGRAY));
        setContentView(layout);
    }

    @Override protected void onResume() { super.onResume(); rendered = -1; refresh.run(); }
    @Override protected void onPause() { handler.removeCallbacks(refresh); super.onPause(); }

    private void render() {
        boolean granted = getSystemService(NotificationManager.class)
            .isNotificationListenerAccessGranted(new ComponentName(this, Listener.class));
        String state = !granted ? "通知使用权：未授权" : BridgeState.listening
            ? "通知监听：已连接" : "通知监听：未连接，请检查系统授权";
        state += history.target().isEmpty() ? "\n目标：未配置，请先运行电脑端通知检查"
            : "\n目标：已配置（精确昵称匹配）";
        state += BridgeState.usb ? " · 电脑：已连接" : " · 电脑：未连接";
        List<HistoryStore.Entry> items = history.snapshot(System.currentTimeMillis());
        if (history.storageError()) state += "\n历史更改未保存，重启后可能丢失新记录或恢复旧记录";
        status.setText(state);
        heading.setText("符合条件的通知历史 · " + items.size() + " 条");
        clear.setEnabled(!items.isEmpty() || history.storageError());
        if (rendered == history.revision()) return;
        rendered = history.revision();
        rows.removeAllViews();
        if (items.isEmpty()) {
            rows.addView(text(history.target().isEmpty()
                ? "尚未配置目标\n先在电脑运行 notification-check，手机会记住目标筛选条件。"
                : "暂无符合条件的通知\n目标的新通知到达后会自动显示；请确认抖音允许展示消息通知。",
                15, Color.DKGRAY));
            return;
        }
        SimpleDateFormat date = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault());
        for (HistoryStore.Entry item : items) {
            LinearLayout card = new LinearLayout(this);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setPadding(dp(12), dp(8), dp(12), dp(8));
            card.setBackgroundColor(Color.WHITE);
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
            params.setMargins(0, 0, 0, dp(10));
            rows.addView(card, params);
            card.addView(text(date.format(new Date(item.time))
                + (item.active ? " · 接入时已有通知" : " · 实时通知"), 12, Color.DKGRAY));
            TextView name = text(item.title.isEmpty() ? "抖音通知" : item.title, 16, Color.BLACK);
            name.setTypeface(null, Typeface.BOLD);
            card.addView(name);
            card.addView(text(item.text.isEmpty() ? "通知未提供正文" : item.text, 15, Color.DKGRAY));
        }
    }
}
