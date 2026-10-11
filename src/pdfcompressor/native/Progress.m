#import <Cocoa/Cocoa.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        NSPanel *panel = [[NSPanel alloc] initWithContentRect:NSMakeRect(0, 0, 380, 150)
            styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
        panel.title = @"PDF 压缩";
        panel.level = NSFloatingWindowLevel;
        panel.releasedWhenClosed = NO;
        panel.hidesOnDeactivate = NO;
        NSTextField *heading = [NSTextField labelWithString:@"正在压缩 PDF"];
        heading.font = [NSFont systemFontOfSize:15 weight:NSFontWeightSemibold];
        heading.frame = NSMakeRect(24, 108, 332, 22);
        NSTextField *filename = [NSTextField labelWithString:@""];
        filename.font = [NSFont systemFontOfSize:12];
        filename.textColor = NSColor.secondaryLabelColor;
        filename.lineBreakMode = NSLineBreakByTruncatingMiddle;
        filename.frame = NSMakeRect(24, 83, 332, 18);
        NSProgressIndicator *bar = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(24, 56, 332, 8)];
        bar.style = NSProgressIndicatorStyleBar;
        bar.minValue = 0;
        bar.maxValue = 1;
        bar.indeterminate = YES;
        [bar startAnimation:nil];
        NSTextField *detail = [NSTextField labelWithString:@"正在准备…"];
        detail.font = [NSFont systemFontOfSize:11];
        detail.textColor = NSColor.secondaryLabelColor;
        detail.frame = NSMakeRect(24, 22, 332, 20);
        for (NSView *view in @[heading, filename, bar, detail]) [panel.contentView addSubview:view];
        [panel center];
        [panel orderFrontRegardless];
        // Stdin is owned by compression. EOF terminates this transient process,
        // including if its parent exits unexpectedly. No polling or daemon.
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            char *line = NULL;
            size_t capacity = 0;
            ssize_t length;
            while ((length = getline(&line, &capacity, stdin)) > 0) {
                @autoreleasepool {
                    NSData *data = [NSData dataWithBytes:line length:(NSUInteger)length];
                    id decoded = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
                    if (![decoded isKindOfClass:NSDictionary.class]) continue;
                    NSDictionary *event = decoded;
                    dispatch_async(dispatch_get_main_queue(), ^{
                        if ([event[@"title"] isKindOfClass:NSString.class]) heading.stringValue = event[@"title"];
                        if ([event[@"file"] isKindOfClass:NSString.class]) filename.stringValue = event[@"file"];
                        if ([event[@"stage"] isKindOfClass:NSString.class]) detail.stringValue = event[@"stage"];
                        double total = [event[@"total"] respondsToSelector:@selector(doubleValue)] ? [event[@"total"] doubleValue] : 0;
                        double completed = [event[@"completed"] respondsToSelector:@selector(doubleValue)] ? [event[@"completed"] doubleValue] : 0;
                        if (total > 0) {
                            if (bar.indeterminate) {
                                [bar stopAnimation:nil];
                                bar.indeterminate = NO;
                            }
                            bar.doubleValue = fmax(0, fmin(1, completed / total));
                        } else {
                            if (!bar.indeterminate) {
                                bar.indeterminate = YES;
                                [bar startAnimation:nil];
                            }
                        }
                    });
                }
            }
            free(line);
            dispatch_async(dispatch_get_main_queue(), ^{ [app terminate:nil]; });
        });
        [app run];
    }
    return 0;
}
