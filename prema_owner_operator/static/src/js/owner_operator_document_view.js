/** @odoo-module **/

import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { url } from "@web/core/utils/urls";
import { useFileViewer } from "@web/core/file_viewer/file_viewer_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Read-only button that previews an application document through the module's
 * private delivery route. The attachment never gets a public /web/content URL
 * or an access token: the viewer loads the same scoped route the backend uses,
 * and that route still enforces the reviewer group and record access.
 */
export class OwnerOperatorDocumentView extends Component {
    static template = "prema_owner_operator.OwnerOperatorDocumentView";
    static props = {
        ...standardFieldProps,
        route: { type: String, optional: true },
    };

    setup() {
        this.fileViewer = useFileViewer();
    }

    get hasFile() {
        return Boolean(this.props.record.data[this.props.name]);
    }

    get documentId() {
        return this.props.record.resId;
    }

    get mimetype() {
        return this.props.record.data.mimetype || "";
    }

    get isPdf() {
        return this.mimetype.startsWith("application/pdf");
    }

    get isImage() {
        return this.mimetype.startsWith("image/");
    }

    get isPreviewable() {
        return this.isPdf || this.isImage;
    }

    get fileName() {
        return this.props.record.data.file_name || _t("Document");
    }

    get fileRoute() {
        const route = this.props.route || "document";
        return `/owner_operator/${route}/${this.documentId}`;
    }

    get fileSource() {
        const source = url(this.fileRoute);
        if (this.isPdf) {
            return `/web/static/lib/pdfjs/web/viewer.html?file=${encodeURIComponent(
                source
            )}#pagemode=none`;
        }
        return source;
    }

    get downloadRoute() {
        return url(this.fileRoute, { download: 1 });
    }

    get buttonLabel() {
        return this.isPreviewable ? _t("View") : _t("Download");
    }

    get buttonIcon() {
        return this.isPreviewable ? "fa-eye" : "fa-download";
    }

    get file() {
        return {
            filename: this.fileName,
            name: this.fileName,
            displayName: this.fileName,
            mimetype: this.mimetype,
            isImage: this.isImage,
            isPdf: this.isPdf,
            isViewable: true,
            defaultSource: this.fileSource,
            downloadUrl: this.downloadRoute,
        };
    }

    onClick() {
        if (!this.hasFile || !this.documentId) {
            return;
        }
        if (this.isPreviewable) {
            this.fileViewer.open(this.file);
            return;
        }
        // Unsupported formats are offered as a download through the same
        // private route; the server also forces attachment disposition.
        const link = document.createElement("a");
        link.href = this.downloadRoute;
        link.download = this.fileName;
        document.body.appendChild(link);
        link.click();
        link.remove();
    }
}

export const ownerOperatorDocumentView = {
    component: OwnerOperatorDocumentView,
    displayName: _t("Document preview"),
    supportedTypes: ["boolean"],
    supportedOptions: [
        {
            label: _t("Private route name"),
            name: "route",
            type: "string",
        },
    ],
    extractProps: (fieldInfo) => ({
        route: (fieldInfo.options && fieldInfo.options.route) || "document",
    }),
};

registry.category("fields").add(
    "owner_operator_document_view",
    ownerOperatorDocumentView
);
