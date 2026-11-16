/*
 * RPGCreator Native mruby Host Bridge (C-ABI for Python ctypes).
 * Provides an isolated mruby bytecode VM with bidirectional host callbacks
 * so Ruby RGSS1/RGSS3 scripts execute natively while delegating Graphics, Input,
 * Audio, Marshal, and sandboxed file/RTP I/O to RPGCreator.
 */
#include <mruby.h>
#include <mruby/compile.h>
#include <mruby/error.h>
#include <mruby/proc.h>
#include <mruby/string.h>
#include <mruby/variable.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define RPGC_EXPORT __declspec(dllexport)
#define RPGC_CALL __cdecl
#else
#define RPGC_EXPORT __attribute__((visibility("default")))
#define RPGC_CALL
#endif

typedef void (RPGC_CALL *rpgc_host_cb_t)(void *user_data, const char *op, const char *payload_utf8, int payload_len);

typedef struct rpgc_mrb_ctx {
    mrb_state *mrb;
    rpgc_host_cb_t cb;
    void *user_data;
    char *reply_buf;
    int reply_len;
    int reply_cap;
    int has_error;
} rpgc_mrb_ctx_t;

static void ensure_reply_cap(rpgc_mrb_ctx_t *ctx, int needed) {
    if (needed + 1 <= ctx->reply_cap) {
        return;
    }
    int new_cap = ctx->reply_cap > 0 ? ctx->reply_cap : 4096;
    while (new_cap < needed + 1) {
        new_cap *= 2;
    }
    char *next = (char *)realloc(ctx->reply_buf, (size_t)new_cap);
    if (next) {
        ctx->reply_buf = next;
        ctx->reply_cap = new_cap;
    }
}

RPGC_EXPORT void RPGC_CALL rpgc_mrb_set_reply(void *raw_ctx, const char *data, int len) {
    rpgc_mrb_ctx_t *ctx = (rpgc_mrb_ctx_t *)raw_ctx;
    if (!ctx || len < 0) return;
    ensure_reply_cap(ctx, len);
    if (ctx->reply_buf && data && len > 0) {
        memcpy(ctx->reply_buf, data, (size_t)len);
    }
    if (ctx->reply_buf) {
        ctx->reply_buf[len] = '\0';
    }
    ctx->reply_len = len;
    ctx->has_error = 0;
}

RPGC_EXPORT void RPGC_CALL rpgc_mrb_set_error(void *raw_ctx, const char *data, int len) {
    rpgc_mrb_ctx_t *ctx = (rpgc_mrb_ctx_t *)raw_ctx;
    if (!ctx || len < 0) return;
    ensure_reply_cap(ctx, len);
    if (ctx->reply_buf && data && len > 0) {
        memcpy(ctx->reply_buf, data, (size_t)len);
    }
    if (ctx->reply_buf) {
        ctx->reply_buf[len] = '\0';
    }
    ctx->reply_len = len;
    ctx->has_error = 1;
}

static mrb_value mrb_rpgc_host_call(mrb_state *mrb, mrb_value self) {
    (void)self;
    const char *op = NULL;
    mrb_value payload_val = mrb_nil_value();
    mrb_get_args(mrb, "z|S", &op, &payload_val);

    rpgc_mrb_ctx_t *ctx = (rpgc_mrb_ctx_t *)mrb->ud;
    if (!ctx || !ctx->cb) {
        return mrb_nil_value();
    }

    ctx->reply_len = 0;
    ctx->has_error = 0;
    const char *p_ptr = "";
    int p_len = 0;
    if (mrb_string_p(payload_val)) {
        p_ptr = RSTRING_PTR(payload_val);
        p_len = (int)RSTRING_LEN(payload_val);
    }

    ctx->cb(ctx->user_data, op ? op : "", p_ptr, p_len);
    if (ctx->has_error) {
        const char *err = (ctx->reply_buf && ctx->reply_len > 0) ? ctx->reply_buf : "Host callback error";
        mrb_raise(mrb, E_RUNTIME_ERROR, err);
    }
    if (!ctx->reply_buf || ctx->reply_len <= 0) {
        return mrb_str_new_cstr(mrb, "");
    }
    return mrb_str_new(mrb, ctx->reply_buf, (mrb_int)ctx->reply_len);
}

RPGC_EXPORT void* RPGC_CALL rpgc_mrb_create(rpgc_host_cb_t cb, void *user_data) {
    rpgc_mrb_ctx_t *ctx = (rpgc_mrb_ctx_t *)calloc(1, sizeof(rpgc_mrb_ctx_t));
    if (!ctx) return NULL;
    mrb_state *mrb = mrb_open();
    if (!mrb) {
        free(ctx);
        return NULL;
    }
    ctx->mrb = mrb;
    ctx->cb = cb;
    ctx->user_data = user_data;
    mrb->ud = ctx;

    struct RClass *host_mod = mrb_define_module(mrb, "RPGCreatorHost");
    mrb_define_module_function(mrb, host_mod, "_call", mrb_rpgc_host_call, MRB_ARGS_ARG(1, 1));
    return ctx;
}

RPGC_EXPORT void RPGC_CALL rpgc_mrb_destroy(void *raw_ctx) {
    rpgc_mrb_ctx_t *ctx = (rpgc_mrb_ctx_t *)raw_ctx;
    if (!ctx) return;
    if (ctx->mrb) {
        mrb_close(ctx->mrb);
    }
    free(ctx->reply_buf);
    free(ctx);
}

static void write_cstr_buf(char *out_buf, int out_cap, const char *src, int len) {
    if (!out_buf || out_cap <= 0) return;
    int copy_n = len < (out_cap - 1) ? len : (out_cap - 1);
    if (copy_n > 0 && src) {
        memcpy(out_buf, src, (size_t)copy_n);
    }
    out_buf[copy_n] = '\0';
}

RPGC_EXPORT int RPGC_CALL rpgc_mrb_eval(
    void *raw_ctx,
    const char *filename,
    const char *code,
    int code_len,
    char *out_buf,
    int out_cap
) {
    rpgc_mrb_ctx_t *ctx = (rpgc_mrb_ctx_t *)raw_ctx;
    if (!ctx || !ctx->mrb) {
        write_cstr_buf(out_buf, out_cap, "Invalid mruby context", 21);
        return 2;
    }
    mrb_state *mrb = ctx->mrb;
    mrb->exc = NULL;

    mrbc_context *cxt = mrbc_context_new(mrb);
    mrbc_filename(mrb, cxt, (filename && filename[0]) ? filename : "(script)");
    int arena_idx = mrb_gc_arena_save(mrb);
    mrb_value res = mrb_load_nstring_cxt(mrb, code ? code : "", code_len >= 0 ? (size_t)code_len : 0, cxt);
    mrbc_context_free(mrb, cxt);

    if (mrb->exc) {
        mrb_value exc_val = mrb_obj_value(mrb->exc);
        mrb->exc = NULL;
        mrb_value msg = mrb_funcall(mrb, exc_val, "inspect", 0);
        mrb_value bt = mrb_funcall(mrb, exc_val, "backtrace", 0);
        char combined[8192];
        int used = 0;
        if (mrb_string_p(msg)) {
            int n = (int)RSTRING_LEN(msg);
            if (n > 4000) n = 4000;
            memcpy(combined, RSTRING_PTR(msg), (size_t)n);
            used = n;
        }
        if (mrb_array_p(bt)) {
            mrb_value joined = mrb_funcall(mrb, bt, "join", 1, mrb_str_new_cstr(mrb, "\n  from "));
            if (mrb_string_p(joined) && used + 12 < (int)sizeof(combined)) {
                memcpy(combined + used, "\n  from ", 8);
                used += 8;
                int rem = (int)sizeof(combined) - used - 1;
                int blen = (int)RSTRING_LEN(joined);
                if (blen > rem) blen = rem;
                memcpy(combined + used, RSTRING_PTR(joined), (size_t)blen);
                used += blen;
            }
        }
        combined[used] = '\0';
        write_cstr_buf(out_buf, out_cap, combined, used);
        mrb->exc = NULL;
        mrb_gc_arena_restore(mrb, arena_idx);
        return 1;
    }

    mrb_value out_str = mrb_string_p(res) ? res : mrb_inspect(mrb, res);
    if (mrb_string_p(out_str)) {
        write_cstr_buf(out_buf, out_cap, RSTRING_PTR(out_str), (int)RSTRING_LEN(out_str));
    } else {
        write_cstr_buf(out_buf, out_cap, "nil", 3);
    }
    mrb_gc_arena_restore(mrb, arena_idx);
    return 0;
}

RPGC_EXPORT const char* RPGC_CALL rpgc_mrb_version(void) {
    return MRUBY_DESCRIPTION;
}
