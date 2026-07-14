package kernel

import (
	"fmt"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type Planner struct {
	selector *ModelSelector
}

func NewPlanner(selector *ModelSelector) *Planner {
	if selector == nil {
		selector = NewModelSelector(nil)
	}
	return &Planner{selector: selector}
}

func (p *Planner) Prepare(task domain.Task) (domain.Task, domain.ExecutionPlan) {
	task.RoutingHints = cloneHints(task.RoutingHints)
	task.RequiredCapability = inferCapability(task)
	selection := p.selector.Select(task)
	task.Complexity = selection.Complexity
	if task.AssignedProvider == "" {
		task.AssignedProvider = selection.Provider
	}
	if task.AssignedModel == "" {
		task.AssignedModel = selection.ModelName
	}
	task.RoutingHints["required_capability"] = task.RequiredCapability
	task.RoutingHints["sourcecraft_work"] = isSourcecraftWork(task)
	task.RoutingHints["selected_provider"] = task.AssignedProvider
	task.RoutingHints["selected_model"] = task.AssignedModel
	plan := p.buildPlan(task, selection)
	return task, plan
}

func (p *Planner) buildPlan(task domain.Task, selection domain.ModelSelection) domain.ExecutionPlan {
	steps := p.defaultSteps(task)
	return domain.ExecutionPlan{
		TaskID:            task.ID,
		Complexity:        selection.Complexity,
		PrimaryCapability: task.RequiredCapability,
		Selection:         selection,
		Steps:             steps,
		CreatedAt:         time.Now().UTC(),
	}
}

func (p *Planner) defaultSteps(task domain.Task) []domain.PlanStep {
	files := compactStrings(task.Input.Files)
	mainCapability := task.RequiredCapability
	analyzeID := task.ID + "-analyze"
	steps := []domain.PlanStep{{
		ID:         analyzeID,
		Title:      "analyze task scope and constraints",
		Capability: "plan",
		Files:      files,
	}}

	switch task.Type {
	case domain.TaskTypeCode, domain.TaskTypeFix:
		steps = append(steps, p.codeSteps(task, analyzeID)...)
	case domain.TaskTypePlan:
		steps = append(steps,
			domain.PlanStep{ID: task.ID + "-draft-plan", Title: "draft execution plan", Capability: mainCapability, Dependencies: []string{analyzeID}, Files: files},
			domain.PlanStep{ID: task.ID + "-review-plan", Title: "review planning output", Capability: "review", Dependencies: []string{task.ID + "-draft-plan"}, Files: files},
		)
	case domain.TaskTypeReview:
		steps = append(steps,
			domain.PlanStep{ID: task.ID + "-audit", Title: "audit implementation and risks", Capability: mainCapability, Dependencies: []string{analyzeID}, Files: files},
			domain.PlanStep{ID: task.ID + "-report", Title: "produce review report", Capability: "docs", Dependencies: []string{task.ID + "-audit"}, Files: files},
		)
	case domain.TaskTypeTest:
		steps = append(steps,
			domain.PlanStep{ID: task.ID + "-run-tests", Title: "run test scenarios", Capability: mainCapability, Dependencies: []string{analyzeID}, Files: files},
			domain.PlanStep{ID: task.ID + "-verify-results", Title: "verify failing and passing paths", Capability: "review", Dependencies: []string{task.ID + "-run-tests"}, Files: files},
		)
	case domain.TaskTypeDocs, domain.TaskTypeResearch:
		steps = append(steps,
			domain.PlanStep{ID: task.ID + "-collect-context", Title: "collect supporting context", Capability: mainCapability, Dependencies: []string{analyzeID}, Files: files},
			domain.PlanStep{ID: task.ID + "-write-output", Title: "produce deliverable", Capability: task.RequiredCapability, Dependencies: []string{task.ID + "-collect-context"}, Files: files},
			domain.PlanStep{ID: task.ID + "-review-output", Title: "review deliverable", Capability: "review", Dependencies: []string{task.ID + "-write-output"}, Files: files},
		)
	default:
		steps = append(steps,
			domain.PlanStep{ID: task.ID + "-execute", Title: task.Input.Description, Capability: mainCapability, Dependencies: []string{analyzeID}, Files: files},
			domain.PlanStep{ID: task.ID + "-verify", Title: "verify completion", Capability: "review", Dependencies: []string{task.ID + "-execute"}, Files: files},
		)
	}

	return steps
}

func (p *Planner) codeSteps(task domain.Task, analyzeID string) []domain.PlanStep {
	files := compactStrings(task.Input.Files)
	capability := task.RequiredCapability
	if capability == "sourcecraft" {
		capability = "code"
	}
	parallelizable := len(files) > 1 && (task.Complexity == domain.ComplexityHigh || task.Complexity == domain.ComplexityCritical || len(files) > 2 || len(task.Input.AcceptanceCriteria) > 1)
	if !parallelizable {
		executeID := task.ID + "-implement"
		return []domain.PlanStep{
			{ID: executeID, Title: "implement requested changes", Capability: capability, Dependencies: []string{analyzeID}, Files: files},
			{ID: task.ID + "-review", Title: "review code changes", Capability: "review", Dependencies: []string{executeID}, Files: files},
			{ID: task.ID + "-test", Title: "run validation checks", Capability: "test", Dependencies: []string{task.ID + "-review"}, Files: files},
		}
	}
	steps := make([]domain.PlanStep, 0, len(files)+3)
	branchIDs := make([]string, 0, len(files))
	limit := len(files)
	if limit > 4 {
		limit = 4
	}
	for index, file := range files[:limit] {
		branchID := fmt.Sprintf("%s-branch-%d", task.ID, index+1)
		branchIDs = append(branchIDs, branchID)
		steps = append(steps, domain.PlanStep{
			ID:           branchID,
			Title:        fmt.Sprintf("implement changes for %s", file),
			Capability:   capability,
			Dependencies: []string{analyzeID},
			Files:        []string{file},
		})
	}
	reviewID := task.ID + "-merge-review"
	steps = append(steps,
		domain.PlanStep{ID: reviewID, Title: "merge and review parallel branches", Capability: "review", Dependencies: branchIDs, Files: files},
		domain.PlanStep{ID: task.ID + "-test", Title: "run validation checks", Capability: "test", Dependencies: []string{reviewID}, Files: files},
	)
	return steps
}
