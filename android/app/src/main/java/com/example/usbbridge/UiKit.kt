package com.example.usbbridge

import android.content.Context
import android.widget.LinearLayout
import androidx.annotation.ColorRes
import androidx.annotation.DimenRes
import androidx.core.content.ContextCompat

/**
 * Helpers shared by the two activities. The UI is built in Kotlin rather than layout XML, so
 * these are the only place the dimension/colour tokens are resolved.
 *
 * They are Context extensions, which means call sites inside an Activity or inside a View's
 * `apply { }` block resolve them from the enclosing Activity without any plumbing.
 */

/** Resolve a dimens token to pixels. */
internal fun Context.dp(@DimenRes resId: Int): Int = resources.getDimensionPixelSize(resId)

/** Resolve a colour token for the current light/dark configuration. */
internal fun Context.color(@ColorRes resId: Int): Int = ContextCompat.getColor(this, resId)

/** Full-width, content-height params for a child of a vertical LinearLayout. */
internal fun matchWidth(top: Int = 0, bottom: Int = 0) = LinearLayout.LayoutParams(
    LinearLayout.LayoutParams.MATCH_PARENT,
    LinearLayout.LayoutParams.WRAP_CONTENT).apply {
    topMargin = top
    bottomMargin = bottom
}
