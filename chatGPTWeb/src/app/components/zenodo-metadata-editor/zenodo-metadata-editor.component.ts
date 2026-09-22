import {Component, Input, Output, EventEmitter, OnChanges,SimpleChanges ,  ViewChild, ElementRef} from '@angular/core';
import { FormBuilder, FormGroup, FormControl, Validators, FormArray, AbstractControl } from '@angular/forms';

type JsonSchema = {
    type?: 'string' | 'array' | 'object' | 'number' | 'boolean' | any;
    enum?: string[];
    required?: boolean;                 // ✅ add this
    required_if?: Record<string, any>;
    properties?: Record<string, JsonSchema>;
    items?: JsonSchema | Record<string, any>;
    format?: string;
};



type Entry = { key: string; value: JsonSchema };

@Component({
    selector: 'app-zenodo-metadata-editor',
    templateUrl: './zenodo-metadata-editor.component.html',
    styleUrls: ['./zenodo-metadata-editor.component.css', '../../app.component.css']
})
export class ZenodoMetadataEditorComponent implements OnChanges {
    @ViewChild('scrollHost', { static: true }) scrollHost!: ElementRef<HTMLElement>;
    @Input() template: any;
    @Input() saveLabel: string = 'Save';
    @Input() draft: any;
    @Output() save = new EventEmitter<any>();
    @Output() cancel = new EventEmitter<void>();

    form!: FormGroup;
    trackByIndex = (i: number) => i;


    // ✅ this is what the template will iterate (typed)
    schemaRoot: Record<string, JsonSchema> = {};

    constructor(private fb: FormBuilder) {}

    private entriesCache = new WeakMap<object, Entry[]>();

    entriesOf(obj: Record<string, JsonSchema> | null | undefined): Entry[] {
        if (!obj) return [];
        const cached = this.entriesCache.get(obj);
        if (cached) return cached;

        const entries = Object.entries(obj).map(([key, value]) => ({ key, value }));
        this.entriesCache.set(obj, entries);
        return entries;
    }

    trackByEntryKey(_i: number, e: Entry) {
        return e.key;
    }



    private normalizeSchema(s: any): JsonSchema {
        // shorthand: "string", "string?", "number?", ...
        if (typeof s === 'string') {
            const optional = s.endsWith('?');
            const base = optional ? s.slice(0, -1) : s;

            const type =
                base === 'string' ? 'string' :
                    base === 'number' ? 'number' :
                        base === 'boolean' ? 'boolean' :
                            base === 'array' ? 'array' :
                                base === 'object' ? 'object' :
                                    'string';

            return { type, required: optional ? false : undefined };
        }

        if (!s || typeof s !== 'object') return { type: 'string' };

        // normalize union types like ["boolean","object"]
        if (Array.isArray(s.type)) {
            const t =
                s.type.includes('string') ? 'string' :
                    s.type.includes('number') ? 'number' :
                        s.type.includes('boolean') ? 'boolean' :
                            s.type.includes('array') ? 'array' :
                                'object';
            s = { ...s, type: t };
        }

        // normalize arrays with shorthand map items
        if (s.type === 'array' && s.items && typeof s.items === 'object' && !Array.isArray(s.items)) {
            const items: any = s.items;

            // items: { name: "string", affiliation: "string?" }  => object schema
            if (!items.type && !items.properties) {
                const props: Record<string, JsonSchema> = {};
                for (const k of Object.keys(items)) props[k] = this.normalizeSchema(items[k]);
                return { ...s, items: { type: 'object', properties: props } };
            }

            return { ...s, items: this.normalizeSchema(items) };
        }

        // normalize object properties recursively
        if (s.type === 'object' && s.properties) {
            const props: Record<string, JsonSchema> = {};
            for (const k of Object.keys(s.properties)) props[k] = this.normalizeSchema(s.properties[k]);
            return { ...s, properties: props };
        }

        return s;
    }


    isObjectArray(e: Entry): boolean {
        const items = e.value.items as any;
        return !!items && typeof items === 'object' && items.type === 'object';
    }

    objectItemSchema(e: Entry): Record<string, JsonSchema> {
        const items = e.value.items as any;
        return (items?.properties ?? {}) as Record<string, JsonSchema>;
    }

    private normalizeRootSchema(root: Record<string, any>): Record<string, JsonSchema> {
        const out: Record<string, JsonSchema> = {};
        for (const k of Object.keys(root || {})) out[k] = this.normalizeSchema(root[k]);
        return out;
    }

    getFormArray(group: AbstractControl, key: string): FormArray {
        return group.get(key) as FormArray;
    }
    private findScrollParent(el: HTMLElement | null): HTMLElement | null {
        let p: HTMLElement | null = el;
        while (p) {
            const s = getComputedStyle(p);
            const scrollable = /(auto|scroll)/.test(s.overflowY);
            if (scrollable && p.scrollHeight > p.clientHeight) return p;
            p = p.parentElement;
        }
        return null;
    }


    addArrayItemBySchema(group: AbstractControl, e: Entry, ev?: Event) {
        ev?.preventDefault();
        ev?.stopPropagation();

        const clickedEl = ev?.target as HTMLElement | undefined;
        const scroller = this.findScrollParent(clickedEl ?? null) ?? (document.scrollingElement as HTMLElement | null);
        const prevTop = scroller?.scrollTop ?? 0;

        const arr = this.getFormArray(group, e.key);
        const itemSchema = e.value.items as JsonSchema | undefined;

        if (itemSchema?.type === 'object') {
            const props = itemSchema.properties ?? {};
            arr.push(this.fb.group(this.buildGroup(props, {})));
        } else {
            arr.push(new FormControl(''));
        }

        const newIndex = arr.length - 1;
        const selector = `[data-array-item="${e.key}:${newIndex}"]`;

        // Wait until Angular renders the new item
        requestAnimationFrame(() => {
            // restore (prevents “jump to top”)
            if (scroller) scroller.scrollTop = prevTop;

            // then scroll precisely to the new item
            requestAnimationFrame(() => {
                const el = document.querySelector(selector) as HTMLElement | null;
                if (!el) return;

                el.scrollIntoView({ block: 'center', behavior: 'auto' });

                // focus first input inside the new item
                const first = el.querySelector('input, textarea, select') as HTMLElement | null;
                first?.focus();
            });
        });
    }






    removeArrayItemByKey(group: AbstractControl, key: string, index: number) {
        this.getFormArray(group, key).removeAt(index);
    }



    ngOnChanges(changes: SimpleChanges) {
        console.log('ngOnChanges', Object.keys(changes));

        // 1) Template changed => rebuild schema + rebuild form
        if (changes['template'] && this.template) {
            const rawSchema = (this.template?.metadata ?? this.template) as Record<string, any>;
            const schema = this.normalizeRootSchema(rawSchema);
            this.schemaRoot = schema;

            const values = this.draft ?? {};
            this.form = this.fb.group(this.buildGroup(schema, values));

            this.form.valueChanges.subscribe(v => this.applyConditionalRules(schema, v, this.form));
            this.applyConditionalRules(schema, this.form.value, this.form);

            return; // IMPORTANT: stop here
        }

        // 2) Draft changed but template didn't => patch scalar controls, but REBUILD
        // FormArrays so that newly-arrived items (e.g. keywords) are correctly
        // populated. FormArray.patchValue() only patches existing indices — it
        // never adds new ones, so an array that was built empty stays empty.
        if (changes['draft'] && this.form) {
            const values = this.draft ?? {};
            this._syncArrayControls(this.form, this.schemaRoot, values);
            this.form.patchValue(values, { emitEvent: false });
        }
    }

    /**
     * For every array field in the schema, replace the FormArray contents so
     * that the number of controls matches the incoming values array.
     * This is necessary because FormArray.patchValue() does NOT add/remove items.
     */
    private _syncArrayControls(
        group: FormGroup,
        schema: Record<string, JsonSchema>,
        values: any,
    ) {
        for (const [key, rule] of Object.entries(schema || {})) {
            if (rule.type !== 'array') continue;
            const arr = group.get(key) as FormArray | null;
            if (!arr) continue;

            const newVals: any[] = Array.isArray(values?.[key]) ? values[key] : [];
            if (arr.length === newVals.length) continue; // nothing to do

            // Clear and repopulate
            while (arr.length > 0) arr.removeAt(0, { emitEvent: false });
            for (const x of newVals) {
                arr.push(this.buildControl(rule, x), { emitEvent: false });
            }
        }
    }


    onSave() {
        if (!this.form) return;

        if (this.form.invalid) {
            this.form.markAllAsTouched();

            // ⬇️ Scroll to first invalid control
            const firstInvalid = document.querySelector(
                '.ng-invalid[formcontrolname], .ng-invalid[formarrayname]'
            ) as HTMLElement | null;

            if (firstInvalid) {
                firstInvalid.scrollIntoView({
                    behavior: 'smooth',
                    block: 'center',
                });

                // focus if possible
                const input = firstInvalid.querySelector('input, textarea, select') as HTMLElement | null;
                input?.focus();
            }

            return;
        }

        console.log('Zenodo metadata form (raw):', this.form.value);

        const cleaned = this.removeEmptyValues(this.form.value);

        console.log('Zenodo metadata form (cleaned):', cleaned);

        this.save.emit(cleaned);
    }
    private removeEmptyValues(obj: any): any {
        if (Array.isArray(obj)) {
            return obj
                .map(v => this.removeEmptyValues(v))
                .filter(v =>
                    v !== undefined &&
                    v !== null &&
                    v !== '' &&
                    !(Array.isArray(v) && v.length === 0)
                );
        }

        if (obj && typeof obj === 'object') {
            const out: any = {};
            Object.entries(obj).forEach(([k, v]) => {
                const cleaned = this.removeEmptyValues(v);
                if (
                    cleaned !== undefined &&
                    cleaned !== null &&
                    cleaned !== '' &&
                    !(Array.isArray(cleaned) && cleaned.length === 0)
                ) {
                    out[k] = cleaned;
                }
            });
            return out;
        }

        return obj;
    }



    // ===== form helpers =====

    getArray(ctrl: AbstractControl | null): FormArray {
        return (ctrl as FormArray) ?? this.fb.array([]);
    }

    addArrayItem(ctrl: AbstractControl | null) {
        this.getArray(ctrl).push(new FormControl(''));
    }

    removeArrayItem(ctrl: AbstractControl | null, index: number) {
        this.getArray(ctrl).removeAt(index);
    }

    // ===== build controls from schema =====

    private buildGroup(schemaObj: Record<string, JsonSchema>, valuesObj: any): Record<string, any> {
        const group: Record<string, any> = {};
        for (const key of Object.keys(schemaObj || {})) {
            const rule = schemaObj[key];
            const initial = valuesObj?.[key];
            group[key] = this.buildControl(rule, initial);
        }
        return group;
    }

    private buildControl(rule: JsonSchema, initial: any): AbstractControl {
        if (!rule || typeof rule !== 'object') return new FormControl(initial ?? null);

        // enum -> select
        if (Array.isArray(rule.enum)) {
            return new FormControl(
                typeof initial === 'string' ? initial : (rule.enum?.[0] ?? null)
            );
        }

        const t = rule.type;

        // ✅ defensive: if schema says primitive but initial is object, ignore it
        const isBadPrimitive =
            (t === 'string' || t === 'number' || t === 'boolean') &&
            initial !== null &&
            typeof initial === 'object';

        if (isBadPrimitive) initial = null;

        if (t === 'string') return new FormControl(initial ?? null);
        if (t === 'number') return new FormControl(initial ?? null);
        if (t === 'boolean') return new FormControl(!!initial);

        if (t === 'array') {
            const arr = Array.isArray(initial) ? initial : [];
            const itemSchema = rule.items as JsonSchema | undefined;

            if (itemSchema?.type === 'object') {
                const props = itemSchema.properties ?? {};
                return this.fb.array(arr.map(item => this.fb.group(this.buildGroup(props, item ?? {}))));
            }

            return this.fb.array(arr.map(x => new FormControl(x ?? null)));
        }

        if (t === 'object') {
            const props = rule.properties ?? {};
            return this.fb.group(this.buildGroup(props, initial ?? {}));
        }

        return new FormControl(initial ?? null);
    }


    // ===== conditional required rules =====

    private applyConditionalRules(
        schemaObj: Record<string, JsonSchema>,
        currentValue: any,
        formGroup: FormGroup
    ) {
        for (const key of Object.keys(schemaObj || {})) {
            const rule = schemaObj[key];
            const ctrl = formGroup.get(key);
            if (!ctrl) continue;

            const requiredIf = rule?.required_if;
            if (requiredIf && typeof requiredIf === 'object') {
                const ok = Object.entries(requiredIf).every(([depKey, depVal]) => currentValue?.[depKey] === depVal);
                if (ok) ctrl.setValidators([Validators.required]);
                else ctrl.clearValidators();
                ctrl.updateValueAndValidity({ emitEvent: false });
            }

            // recurse object
            if (rule.type === 'object' && ctrl instanceof FormGroup) {
                this.applyConditionalRules(rule.properties ?? {}, currentValue?.[key] ?? {}, ctrl);
            }
        }
    }
}
