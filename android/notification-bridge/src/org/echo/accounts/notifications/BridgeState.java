package org.echo.accounts.notifications;

import android.content.Context;
import java.io.File;

final class BridgeState {
    private static HistoryStore history;
    static volatile boolean listening, usb;
    static synchronized HistoryStore history(Context context) {
        if (history == null) history = new HistoryStore(
            new File(context.getNoBackupFilesDir(), "notification-history.bin"),
            System.currentTimeMillis());
        return history;
    }
}
