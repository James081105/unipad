#import <UIKit/UIKit.h>

#ifndef UNIPAD_NOTES
#define UNIPAD_NOTES 0
#endif

@interface SampleController : UIViewController <UITextFieldDelegate>
@property(nonatomic, strong) UILabel *challengeLabel;
@property(nonatomic, strong) UILabel *countLabel;
@property(nonatomic, strong) UILabel *statusLabel;
@property(nonatomic, strong) UITextField *challengeField;
@property(nonatomic, strong) UITextView *noteField;
@property(nonatomic, copy) NSString *challenge;
@property(nonatomic) NSUInteger count;
- (void)acceptChallenge:(NSString *)value;
@end

@implementation SampleController

- (UILabel *)label:(NSString *)text identifier:(NSString *)identifier {
    UILabel *label = [[UILabel alloc] init];
    label.text = text;
    label.numberOfLines = 0;
    label.font = [UIFont systemFontOfSize:20];
    label.accessibilityIdentifier = identifier;
    return label;
}

- (UIButton *)button:(NSString *)title action:(SEL)action identifier:(NSString *)identifier {
    UIButton *button = [UIButton buttonWithType:UIButtonTypeSystem];
    [button setTitle:title forState:UIControlStateNormal];
    button.titleLabel.font = [UIFont boldSystemFontOfSize:22];
    button.accessibilityIdentifier = identifier;
    [button addTarget:self action:action forControlEvents:UIControlEventTouchUpInside];
    [button.heightAnchor constraintGreaterThanOrEqualToConstant:52].active = YES;
    return button;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = UIColor.systemBackgroundColor;
    UIScrollView *scroll = [[UIScrollView alloc] init];
    scroll.translatesAutoresizingMaskIntoConstraints = NO;
    scroll.keyboardDismissMode = UIScrollViewKeyboardDismissModeOnDrag;
    [self.view addSubview:scroll];
    UIStackView *stack = [[UIStackView alloc] init];
    stack.axis = UILayoutConstraintAxisVertical;
    stack.spacing = 16;
    stack.translatesAutoresizingMaskIntoConstraints = NO;
    [scroll addSubview:stack];
    UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[
        [scroll.topAnchor constraintEqualToAnchor:safe.topAnchor],
        [scroll.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor],
        [scroll.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor],
        [scroll.bottomAnchor constraintEqualToAnchor:self.view.keyboardLayoutGuide.topAnchor],
        [stack.topAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.topAnchor constant:24],
        [stack.leadingAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.leadingAnchor constant:24],
        [stack.trailingAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.trailingAnchor constant:-24],
        [stack.bottomAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.bottomAnchor constant:-24],
        [stack.widthAnchor constraintEqualToAnchor:scroll.frameLayoutGuide.widthAnchor constant:-48],
    ]];
    UILabel *title = [self label:UNIPAD_NOTES ? @"UniPadNotes" : @"HelloUniPad" identifier:@"appTitle"];
    title.font = [UIFont boldSystemFontOfSize:32];
    [stack addArrangedSubview:title];
    self.challengeLabel = [self label:@"" identifier:@"challengeValue"];
    self.challengeLabel.font = [UIFont monospacedSystemFontOfSize:18 weight:UIFontWeightRegular];
    [stack addArrangedSubview:self.challengeLabel];
    self.challengeField = [[UITextField alloc] init];
    self.challengeField.borderStyle = UITextBorderStyleRoundedRect;
    self.challengeField.placeholder = @"Enter this run's challenge";
    self.challengeField.accessibilityIdentifier = @"challengeInput";
    self.challengeField.autocorrectionType = UITextAutocorrectionTypeNo;
    self.challengeField.autocapitalizationType = UITextAutocapitalizationTypeNone;
    self.challengeField.returnKeyType = UIReturnKeyDone;
    self.challengeField.delegate = self;
    [self.challengeField.heightAnchor constraintEqualToConstant:48].active = YES;
    [stack addArrangedSubview:self.challengeField];
    [stack addArrangedSubview:[self button:@"Apply challenge" action:@selector(applyChallenge) identifier:@"applyChallenge"]];
    if (!self.challenge) {
        self.challenge = NSUUID.UUID.UUIDString;
    }
    [self acceptChallenge:self.challenge];
    self.statusLabel = [self label:@"" identifier:@"operationStatus"];
    if (UNIPAD_NOTES) {
        self.noteField = [[UITextView alloc] init];
        self.noteField.font = [UIFont systemFontOfSize:22];
        self.noteField.accessibilityIdentifier = @"noteInput";
        self.noteField.backgroundColor = UIColor.secondarySystemBackgroundColor;
        [self.noteField.heightAnchor constraintEqualToConstant:180].active = YES;
        [stack addArrangedSubview:self.noteField];
        [stack addArrangedSubview:[self button:@"Save note" action:@selector(saveNote) identifier:@"saveNote"]];
        [stack addArrangedSubview:[self button:@"Reload saved note" action:@selector(loadNote) identifier:@"reloadNote"]];
        [self loadNote];
    } else {
        self.countLabel = [self label:@"Count: 0" identifier:@"clickCount"];
        self.countLabel.font = [UIFont monospacedDigitSystemFontOfSize:30 weight:UIFontWeightBold];
        [stack addArrangedSubview:self.countLabel];
        [stack addArrangedSubview:[self button:@"Count tap" action:@selector(countTap) identifier:@"countTap"]];
        self.statusLabel.text = @"Ready";
    }
    [stack addArrangedSubview:self.statusLabel];
}

- (void)acceptChallenge:(NSString *)value {
    if (value.length == 0 || value.length > 256) {
        return;
    }
    self.challenge = value;
    self.challengeLabel.text = [@"Challenge: " stringByAppendingString:value];
}

- (void)applyChallenge {
    NSString *value = self.challengeField.text;
    if (value.length == 0 || value.length > 256) {
        self.statusLabel.text = @"Challenge must contain 1 to 256 characters";
        return;
    }
    [self acceptChallenge:value];
    [self dismissKeyboard];
}

- (BOOL)textFieldShouldReturn:(UITextField *)textField {
    (void)textField;
    [self applyChallenge];
    return YES;
}

- (void)dismissKeyboard {
    [self.view endEditing:YES];
}

- (void)countTap {
    self.count += 1;
    self.countLabel.text = [NSString stringWithFormat:@"Count: %lu", (unsigned long)self.count];
}

- (NSURL *)noteURL {
    NSURL *directory = [NSFileManager.defaultManager URLsForDirectory:NSDocumentDirectory inDomains:NSUserDomainMask].firstObject;
    return [directory URLByAppendingPathComponent:@"note.txt"];
}

- (void)saveNote {
    NSError *error = nil;
    NSURL *url = [self noteURL];
    if (!url) {
        self.statusLabel.text = @"Save failed: Documents directory unavailable";
        return;
    }
    NSString *text = self.noteField.text ?: @"";
    if (![text writeToURL:url atomically:YES encoding:NSUTF8StringEncoding error:&error]) {
        self.statusLabel.text = [@"Save failed: " stringByAppendingString:error.localizedDescription ?: @"unknown error"];
        return;
    }
    NSString *readback = [NSString stringWithContentsOfURL:url encoding:NSUTF8StringEncoding error:&error];
    if (![readback isEqualToString:text]) {
        self.statusLabel.text = @"Save failed: file readback disagrees";
        return;
    }
    self.statusLabel.text = [NSString stringWithFormat:@"Saved and read back %lu characters", (unsigned long)text.length];
    [self dismissKeyboard];
}

- (void)loadNote {
    NSError *error = nil;
    NSURL *url = [self noteURL];
    if (!url) {
        self.statusLabel.text = @"Load failed: Documents directory unavailable";
        return;
    }
    NSString *text = [NSString stringWithContentsOfURL:url encoding:NSUTF8StringEncoding error:&error];
    if (!text) {
        self.statusLabel.text = [error.domain isEqualToString:NSCocoaErrorDomain] && error.code == NSFileReadNoSuchFileError
            ? @"No saved note yet"
            : [@"Load failed: " stringByAppendingString:error.localizedDescription ?: @"unknown error"];
        return;
    }
    self.noteField.text = text;
    self.statusLabel.text = [NSString stringWithFormat:@"Loaded %lu characters from Documents/note.txt", (unsigned long)text.length];
}

@end

@interface SampleDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, strong) SampleController *controller;
@end

@implementation SampleDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)options {
    (void)application;
    self.controller = [[SampleController alloc] init];
    self.window = [[UIWindow alloc] initWithFrame:UIScreen.mainScreen.bounds];
    self.window.rootViewController = self.controller;
    [self.window makeKeyAndVisible];
    NSURL *url = options[UIApplicationLaunchOptionsURLKey];
    if (url) {
        [self application:application openURL:url options:@{}];
    }
    return YES;
}

- (BOOL)application:(UIApplication *)application openURL:(NSURL *)url options:(NSDictionary<UIApplicationOpenURLOptionsKey, id> *)options {
    (void)application;
    (void)options;
    NSString *scheme = UNIPAD_NOTES ? @"unipad-notes" : @"unipad-hello";
    if (![url.scheme isEqualToString:scheme] || ![url.host isEqualToString:@"challenge"]) {
        return NO;
    }
    NSURLComponents *components = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
    for (NSURLQueryItem *item in components.queryItems) {
        if ([item.name isEqualToString:@"value"] && item.value.length > 0 && item.value.length <= 256) {
            [self.controller acceptChallenge:item.value];
            return YES;
        }
    }
    return NO;
}

@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil, NSStringFromClass(SampleDelegate.class));
    }
}
